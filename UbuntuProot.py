from __future__ import annotations

import argparse
import contextlib
import fcntl
import logging
import os
import platform
import pty
import select
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tarfile
import tempfile
import termios
import threading
import time
import traceback
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

try:
    import paramiko
except ImportError:
    subprocess.check_call([sys.executable, "-m", "pip", "install", "paramiko"])
    import paramiko

logging.basicConfig(
    format="[%(asctime)s] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
    level=logging.INFO,
    stream=sys.stdout,
)
log = logging.getLogger(__name__)

ARCH = platform.machine()

_ROOTFS_PRESETS: dict[str, dict[str, str]] = {
    "ubuntu22": {
        "x86_64":  "https://cdimage.ubuntu.com/ubuntu-base/releases/22.04/release/ubuntu-base-22.04-base-amd64.tar.gz",
        "aarch64": "https://cdimage.ubuntu.com/ubuntu-base/releases/22.04/release/ubuntu-base-22.04-base-arm64.tar.gz",
    },
    "ubuntu20": {
        "x86_64":  "https://cdimage.ubuntu.com/ubuntu-base/releases/20.04/release/ubuntu-base-20.04.1-base-amd64.tar.gz",
        "aarch64": "https://cdimage.ubuntu.com/ubuntu-base/releases/20.04/release/ubuntu-base-20.04.1-base-arm64.tar.gz",
    },
    "debian12": {
        "x86_64":  "https://github.com/debuerreotype/docker-debian-artifacts/raw/dist-amd64/bookworm/rootfs.tar.xz",
        "aarch64": "https://github.com/debuerreotype/docker-debian-artifacts/raw/dist-arm64v8/bookworm/rootfs.tar.xz",
    },
    "alpine": {
        "x86_64":  "https://dl-cdn.alpinelinux.org/alpine/v3.19/releases/x86_64/alpine-minirootfs-3.19.1-x86_64.tar.gz",
        "aarch64": "https://dl-cdn.alpinelinux.org/alpine/v3.19/releases/aarch64/alpine-minirootfs-3.19.1-aarch64.tar.gz",
    },
}

_PROOT_URLS: dict[str, str] = {
    "x86_64":  "https://proot.gitlab.io/proot/bin/proot",
    "aarch64": "https://proot.gitlab.io/proot/bin/proot-aarch64",
}

_TTYD_URLS: dict[str, str] = {
    "x86_64":  "https://github.com/tsl0922/ttyd/releases/download/1.7.3/ttyd.x86_64",
    "aarch64": "https://github.com/tsl0922/ttyd/releases/download/1.7.3/ttyd.aarch64",
}

@dataclass
class ServerConfig:
    port: int = 30002
    ttyd_port: int = 17620
    bind_host: str = "0.0.0.0"
    advertise_host: str = "127.0.0.1"

    ssh_user: str = "root"
    ssh_pass: str = "password"
    custom_user: str = "user"

    max_connections: int = 50
    proxy_timeout: int = 600
    peek_timeout: float = 3.0

    base_dir: Path = field(default_factory=lambda: Path(__file__).parent.resolve())

    rootfs_source: str = "ubuntu22"

    @property
    def proot_path(self) -> Path:
        return self.base_dir / "proot"

    @property
    def ttyd_path(self) -> Path:
        return self.base_dir / "ttyd"

    @property
    def rootfs_path(self) -> Path:
        return self.base_dir / "rootfs"

    @property
    def key_file(self) -> Path:
        return self.base_dir / "server.key"

    @property
    def custom_rc_path(self) -> Path:
        return self.base_dir / ".custom_rc"

    @property
    def setup_marker(self) -> Path:
        return self.rootfs_path / ".setup_done"

    def resolve_rootfs_url(self) -> Optional[str]:
        src = self.rootfs_source
        if src.startswith("local:"):
            return None
        if src.startswith("http://") or src.startswith("https://"):
            return src
        arch_key = "aarch64" if ARCH in ("aarch64", "arm64") else "x86_64"
        preset = _ROOTFS_PRESETS.get(src)
        if preset is None:
            raise ValueError(f"Unknown rootfs preset: {src!r}. "
                             f"Use one of {list(_ROOTFS_PRESETS)} or 'local:/path' or a URL.")
        url = preset.get(arch_key)
        if url is None:
            raise ValueError(f"Preset {src!r} has no entry for arch {arch_key}")
        return url

    def resolve_local_rootfs_path(self) -> Optional[Path]:
        if self.rootfs_source.startswith("local:"):
            return Path(self.rootfs_source[6:])
        return None

def config_from_args(argv: Optional[list[str]] = None) -> ServerConfig:
    p = argparse.ArgumentParser(description="PRoot SSH/HTTP multiplexer server")

    p.add_argument("--port", type=int,
                   default=int(os.environ.get("PORT", 30002)))
    p.add_argument("--ttyd-port", type=int,
                   default=int(os.environ.get("TTYD_PORT", 17620)))
    p.add_argument("--bind", default=os.environ.get("BIND_HOST", "0.0.0.0"))
    p.add_argument("--advertise-host",
                   default=os.environ.get("ADVERTISE_HOST", "127.0.0.1"))
    p.add_argument("--ssh-user", default=os.environ.get("SSH_USER", "root"))
    p.add_argument("--ssh-pass", default=os.environ.get("SSH_PASS", "password"))
    p.add_argument("--custom-user",
                   default=os.environ.get("CUSTOM_USER", "user"))
    p.add_argument("--max-connections", type=int,
                   default=int(os.environ.get("MAX_CONNECTIONS", 50)))
    p.add_argument("--proxy-timeout", type=int,
                   default=int(os.environ.get("PROXY_TIMEOUT", 600)))
    p.add_argument("--base-dir",
                   default=os.environ.get("BASE_DIR", str(Path(__file__).parent.resolve())))
    p.add_argument("--rootfs",
                   default=os.environ.get("ROOTFS", "ubuntu22"),
                   help="Preset name (ubuntu22/ubuntu20/debian12/alpine), "
                        "'local:/path/to/rootfs.tar.gz', or HTTP(S) URL")
    p.add_argument("--log-level",
                   default=os.environ.get("LOG_LEVEL", "INFO"),
                   choices=["DEBUG", "INFO", "WARNING", "ERROR"])

    ns = p.parse_args(argv)
    logging.getLogger().setLevel(getattr(logging, ns.log_level))

    return ServerConfig(
        port=ns.port,
        ttyd_port=ns.ttyd_port,
        bind_host=ns.bind,
        advertise_host=ns.advertise_host,
        ssh_user=ns.ssh_user,
        ssh_pass=ns.ssh_pass,
        custom_user=ns.custom_user,
        max_connections=ns.max_connections,
        proxy_timeout=ns.proxy_timeout,
        base_dir=Path(ns.base_dir).resolve(),
        rootfs_source=ns.rootfs,
    )

class Downloader:
    def __init__(self, retries: int = 3, retry_delay: float = 3.0) -> None:
        self.retries = retries
        self.retry_delay = retry_delay

    def download(self, url: str, dest: Path) -> bool:
        for attempt in range(1, self.retries + 1):
            try:
                log.info("Downloading %s (attempt %d/%d)", url, attempt, self.retries)
                tmp = dest.with_suffix(dest.suffix + ".part")
                urllib.request.urlretrieve(url, tmp)
                if tmp.exists() and tmp.stat().st_size > 1000:
                    tmp.replace(dest)
                    log.info("Saved %s (%d bytes)", dest, dest.stat().st_size)
                    return True
                log.warning("Downloaded file too small, retrying…")
                tmp.unlink(missing_ok=True)
            except Exception:
                log.warning("Download attempt %d failed:\n%s", attempt, traceback.format_exc())
            time.sleep(self.retry_delay)
        log.error("Giving up on %s", url)
        return False

def _rewrite_member(member: tarfile.TarInfo, dest_path: Path) -> Optional[tarfile.TarInfo]:
    dest_abs = str(dest_path.resolve())

    member_path = os.path.normpath(os.path.join(dest_abs, member.name))
    if not (member_path.startswith(dest_abs + os.sep) or member_path == dest_abs):
        log.warning("Skipping path-traversal member: %s", member.name)
        return None

    if member.issym() and member.linkname.startswith("/"):
        pass

    if member.islnk():
        link_path = os.path.normpath(os.path.join(dest_abs, member.linkname))
        if not (link_path.startswith(dest_abs + os.sep) or link_path == dest_abs):
            log.warning("Skipping unsafe hardlink: %s -> %s", member.name, member.linkname)
            return None

    return member

def _safe_extract_tar(tar_path: Path, dest: Path) -> None:
    mode_map = {".tar.xz": "r:xz", ".tar.gz": "r:gz", ".tar.bz2": "r:bz2"}
    mode = next((v for k, v in mode_map.items() if tar_path.name.endswith(k)), "r:*")

    dest_path = dest.resolve()

    with tarfile.open(tar_path, mode) as tf:
        if hasattr(tarfile, "TarFile") and hasattr(tf, "extraction_filter"):
            def _filter(member: tarfile.TarInfo, path: str) -> Optional[tarfile.TarInfo]:
                return _rewrite_member(member, Path(path))

            tf.extraction_filter = _filter
            tf.extractall(path=dest)
        else:
            members: list[tarfile.TarInfo] = []
            for m in tf.getmembers():
                result = _rewrite_member(m, dest_path)
                if result is not None:
                    members.append(result)
            tf.extractall(path=dest, members=members)

class EnvironmentManager:
    def __init__(self, cfg: ServerConfig) -> None:
        self.cfg = cfg
        self._dl = Downloader()

    def _ensure_binary(self, path: Path, url: str, name: str) -> bool:
        if path.exists():
            log.info("%s already present", name)
            return True
        if not self._dl.download(url, path):
            return False
        path.chmod(0o755)
        return True

    def ensure_proot(self) -> bool:
        arch_key = "aarch64" if ARCH in ("aarch64", "arm64") else "x86_64"
        url = _PROOT_URLS.get(arch_key)
        if url is None:
            log.error("No proot URL for arch %s", ARCH)
            return False
        return self._ensure_binary(self.cfg.proot_path, url, "proot")

    def ensure_ttyd(self) -> bool:
        arch_key = "aarch64" if ARCH in ("aarch64", "arm64") else "x86_64"
        url = _TTYD_URLS.get(arch_key)
        if url is None:
            log.warning("No ttyd URL for arch %s; web terminal disabled", ARCH)
            return False
        return self._ensure_binary(self.cfg.ttyd_path, url, "ttyd")

    def ensure_rootfs(self) -> None:
        cfg = self.cfg
        if cfg.rootfs_path.exists():
            log.info("rootfs already present")
            return

        local_path = cfg.resolve_local_rootfs_path()
        if local_path is not None:
            tar_path = local_path
        else:
            url = cfg.resolve_rootfs_url()
            assert url is not None
            suffix = ".tar.gz" if url.endswith(".gz") else ".tar.xz"
            tar_path = cfg.base_dir / ("rootfs" + suffix)
            if not self._dl.download(url, tar_path):
                raise RuntimeError("Failed to download rootfs")

        log.info("Extracting rootfs…")
        cfg.rootfs_path.mkdir(parents=True, exist_ok=True)
        try:
            _safe_extract_tar(tar_path, cfg.rootfs_path)
        except Exception:
            shutil.rmtree(cfg.rootfs_path, ignore_errors=True)
            raise

        entries = list(cfg.rootfs_path.iterdir())
        if len(entries) == 1 and entries[0].is_dir():
            inner = entries[0]
            tmp = cfg.rootfs_path.parent / (cfg.rootfs_path.name + "_tmp")
            shutil.move(str(inner), str(tmp))
            shutil.rmtree(cfg.rootfs_path)
            shutil.move(str(tmp), str(cfg.rootfs_path))

        if local_path is None:
            tar_path.unlink(missing_ok=True)

        log.info("rootfs ready")

    def write_configs(self) -> None:
        cfg = self.cfg

        resolv = cfg.rootfs_path / "etc" / "resolv.conf"
        resolv.parent.mkdir(parents=True, exist_ok=True)
        resolv.write_text("nameserver 8.8.8.8\nnameserver 1.1.1.1\n")

        rc_text = (
            f"export PS1='\\[\\033[1;32m\\]{cfg.custom_user}\\[\\033[0m\\]@"
            f"\\[\\033[1;34m\\]vps\\[\\033[0m\\]:\\[\\033[1;36m\\]\\w\\[\\033[0m\\]\\$ '\n"
            "export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin\n"
            "export LANG=C.UTF-8\nexport TERM=xterm-256color\n"
            "shopt -s checkwinsize\n"
            "alias ls='ls --color=auto'\nalias ll='ls -la --color=auto'\ncd ~\n"
        )
        cfg.custom_rc_path.write_text(rc_text)
        (cfg.rootfs_path / ".custom_rc").write_text(rc_text)

        root_home = cfg.rootfs_path / "root"
        root_home.mkdir(exist_ok=True)
        (root_home / ".bashrc").write_text("source /.custom_rc\n")

    def remove_apt_locks(self) -> None:
        locks = [
            "var/lib/apt/lists/lock",
            "var/lib/dpkg/lock",
            "var/lib/dpkg/lock-frontend",
            "var/cache/apt/archives/lock",
        ]
        for rel in locks:
            p = self.cfg.rootfs_path / rel
            if p.exists():
                with contextlib.suppress(OSError):
                    p.unlink()

        keyring = self.cfg.rootfs_path / "etc" / "apt" / "trusted.gpg.d"
        if keyring.is_dir():
            for f in keyring.iterdir():
                with contextlib.suppress(OSError):
                    f.chmod(0o644)

    def write_apt_insecure_conf(self) -> None:
        apt_conf = self.cfg.rootfs_path / "etc" / "apt" / "apt.conf.d" / "99proot-settings"
        apt_conf.parent.mkdir(parents=True, exist_ok=True)
        apt_conf.write_text(
            'Acquire::AllowInsecureRepositories "true";\n'
            'Acquire::AllowDowngradeToInsecureRepositories "true";\n'
            'APT::Get::AllowUnauthenticated "true";\n'
        )

    def setup(self) -> None:
        log.info("arch: %s", ARCH)

        if not self.ensure_proot():
            raise RuntimeError("proot unavailable — cannot continue")
        self.ensure_ttyd()

        self.ensure_rootfs()
        self.write_configs()
        self.remove_apt_locks()

        if not self.cfg.setup_marker.exists():
            self._first_run_setup()
        else:
            log.info("Already set up, skipping first-run steps")

        log.info("Environment ready")

    def _first_run_setup(self) -> None:
        log.info("First run: importing GPG key…")
        stdout, stderr, code = self.run_in_proot(
            "gpg --batch --keyserver hkp://keyserver.ubuntu.com:80 "
            "--recv-keys 871920D1991BC93C 2>/dev/null && "
            "gpg --batch --export 871920D1991BC93C "
            "> /etc/apt/trusted.gpg.d/ubuntu-archive-2024.gpg 2>/dev/null; "
            "chmod 644 /etc/apt/trusted.gpg.d/*.gpg 2>/dev/null; echo GPG_OK",
            timeout=30,
        )
        if "GPG_OK" in stdout:
            log.info("GPG key imported")
        else:
            log.warning("GPG import issue: %s", stderr[:200])

        self.write_apt_insecure_conf()
        self.cfg.setup_marker.write_text(str(time.time()))
        log.info("First-run setup done")

    def proot_cmd(self) -> list[str]:
        return [
            str(self.cfg.proot_path.resolve()),
            "-r", str(self.cfg.rootfs_path.resolve()),
            "-0", "-w", "/root",
            "-b", "/dev", "-b", "/proc", "-b", "/sys",
            "/usr/bin/env", "-i",
            "HOME=/root", "TERM=xterm-256color", "LANG=C.UTF-8",
            "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "/bin/bash", "--rcfile", "/.custom_rc",
        ]

    def run_in_proot(
        self,
        bash_cmd: str,
        timeout: int = 60,
    ) -> tuple[str, str, int]:
        cmd = [
            str(self.cfg.proot_path.resolve()),
            "-r", str(self.cfg.rootfs_path.resolve()),
            "-0", "-w", "/root",
            "-b", "/dev", "-b", "/proc", "-b", "/sys",
            "/usr/bin/env", "-i",
            "HOME=/root", "TERM=xterm-256color", "LANG=C.UTF-8",
            "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "/bin/bash", "-c", bash_cmd,
        ]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            return r.stdout, r.stderr, r.returncode
        except subprocess.TimeoutExpired:
            return "", "TIMEOUT", -1
        except Exception as exc:
            return "", str(exc), -1

    def verify_proot(self) -> bool:
        cmd = [
            str(self.cfg.proot_path.resolve()),
            "-r", str(self.cfg.rootfs_path.resolve()),
            "-0", "-w", "/root",
            "-b", "/dev", "-b", "/proc", "-b", "/sys",
            "/usr/bin/env", "-i",
            "HOME=/root", "TERM=xterm-256color", "LANG=C.UTF-8",
            "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "/bin/echo", "PROOT_OK",
        ]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
            ok = "PROOT_OK" in r.stdout
            if ok:
                log.info("proot sanity check passed")
            else:
                log.warning("proot sanity check: unexpected output: %s | %s",
                            r.stdout.strip(), r.stderr.strip())
            return ok
        except Exception:
            log.exception("proot sanity check failed")
            return False

def set_pty_size(fd: int, w: int, h: int) -> None:
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", h, w, 0, 0))
    except OSError:
        pass

class _SSHServerInterface(paramiko.ServerInterface):
    def __init__(self, cfg: ServerConfig) -> None:
        self._cfg = cfg
        self.pty_width: int = 80
        self.pty_height: int = 24
        self.master_fd: Optional[int] = None
        self.shell_requested: threading.Event = threading.Event()

    def check_auth_password(self, username: str, password: str) -> int:
        if username == self._cfg.ssh_user and password == self._cfg.ssh_pass:
            return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED

    def check_channel_request(self, kind: str, chanid: int) -> int:
        if kind == "session":
            return paramiko.OPEN_SUCCEEDED
        return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def check_channel_shell_request(self, channel: paramiko.Channel) -> bool:
        self.shell_requested.set()
        return True

    def check_channel_pty_request(
        self,
        channel: paramiko.Channel,
        term: str,
        width: int,
        height: int,
        pixelwidth: int,
        pixelheight: int,
        modes: bytes,
    ) -> bool:
        self.pty_width = width or 80
        self.pty_height = height or 24
        if self.master_fd is not None:
            set_pty_size(self.master_fd, self.pty_width, self.pty_height)
        return True

    def check_channel_window_change_request(
        self,
        channel: paramiko.Channel,
        width: int,
        height: int,
        pixelwidth: int,
        pixelheight: int,
    ) -> bool:
        self.pty_width = width or self.pty_width
        self.pty_height = height or self.pty_height
        if self.master_fd is not None:
            set_pty_size(self.master_fd, self.pty_width, self.pty_height)
        return True

    def get_allowed_auths(self, username: str) -> str:
        return "password"

class SSHServer:

    def __init__(self, cfg: ServerConfig, env: EnvironmentManager) -> None:
        self._cfg = cfg
        self._env = env

    def handle(self, client_sock: socket.socket, host_key: paramiko.RSAKey) -> None:
        transport: Optional[paramiko.Transport] = None
        chan: Optional[paramiko.Channel] = None
        master_fd: Optional[int] = None
        slave_fd: Optional[int] = None
        proc: Optional[subprocess.Popen] = None

        try:
            transport = paramiko.Transport(client_sock)
            transport.local_version = "SSH-2.0-OpenSSH_8.9"
            transport.add_server_key(host_key)
            ssh_server = _SSHServerInterface(self._cfg)
            transport.start_server(server=ssh_server)

            chan = transport.accept(30)
            if chan is None:
                log.warning("SSH: no channel opened within 30 s")
                return

            if not ssh_server.shell_requested.wait(timeout=10):
                log.warning("SSH: shell request not received within 10 s, aborting")
                return

            master_fd, slave_fd = pty.openpty()
            ssh_server.master_fd = master_fd
            set_pty_size(master_fd, ssh_server.pty_width, ssh_server.pty_height)
            def _set_ctty() -> None:
                os.setsid()
                try:
                    import fcntl as _fcntl, termios as _termios
                    _fcntl.ioctl(slave_fd, _termios.TIOCSCTTY, 0)
                except OSError:
                    pass

            proc = subprocess.Popen(
                self._env.proot_cmd(),
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                start_new_session=False,
                close_fds=True,
                preexec_fn=_set_ctty,
            )
            os.close(slave_fd)
            slave_fd = None

            stop = threading.Event()

            def _chan_to_proc() -> None:
                try:
                    while not stop.is_set() and not chan.closed:
                        data = chan.recv(4096)
                        if not data:
                            break
                        try:
                            os.write(master_fd, data)
                        except OSError as exc:
                            log.debug("SSH c2p write error: %s", exc)
                            break
                except Exception:
                    log.debug("SSH c2p thread error", exc_info=True)
                finally:
                    stop.set()
                    if proc and proc.poll() is None:
                        with contextlib.suppress(OSError):
                            proc.terminate()

            def _proc_to_chan() -> None:
                try:
                    while not stop.is_set():
                        try:
                            r, _, _ = select.select([master_fd], [], [], 0.5)
                        except (ValueError, OSError):
                            break
                        if r:
                            try:
                                data = os.read(master_fd, 4096)
                            except OSError:
                                break
                            if not data:
                                break
                            try:
                                chan.sendall(data)
                            except Exception:
                                break
                        elif proc and proc.poll() is not None:
                            while True:
                                try:
                                    r2, _, _ = select.select([master_fd], [], [], 0.1)
                                except (ValueError, OSError):
                                    break
                                if not r2:
                                    break
                                try:
                                    data = os.read(master_fd, 4096)
                                except OSError:
                                    break
                                if not data:
                                    break
                                with contextlib.suppress(Exception):
                                    chan.sendall(data)
                            break
                except Exception:
                    log.debug("SSH p2c thread error", exc_info=True)
                finally:
                    stop.set()

            t_c2p = threading.Thread(target=_chan_to_proc, daemon=True)
            t_p2c = threading.Thread(target=_proc_to_chan, daemon=True)
            t_c2p.start()
            t_p2c.start()

            proc.wait()
            stop.set()
            t_c2p.join(timeout=3)
            t_p2c.join(timeout=3)

        except paramiko.SSHException as exc:
            log.debug("SSH protocol error: %s", exc)
        except Exception:
            log.debug("SSH session error", exc_info=True)
        finally:
            for obj in (chan, transport):
                if obj is not None:
                    with contextlib.suppress(Exception):
                        obj.close()
            if slave_fd is not None:
                with contextlib.suppress(OSError):
                    os.close(slave_fd)
            if master_fd is not None:
                with contextlib.suppress(OSError):
                    os.close(master_fd)
            if proc is not None and proc.poll() is None:
                with contextlib.suppress(OSError):
                    proc.kill()
                with contextlib.suppress(OSError):
                    proc.wait(timeout=3)
            with contextlib.suppress(Exception):
                client_sock.close()

class TcpProxy:

    def __init__(self, cfg: ServerConfig) -> None:
        self._cfg = cfg

    def relay(self, client_sock: socket.socket, target_port: int) -> None:
        target_sock: Optional[socket.socket] = None
        try:
            target_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            target_sock.settimeout(5)
            target_sock.connect(("127.0.0.1", target_port))
            target_sock.settimeout(None)
            client_sock.settimeout(None)

            stop = threading.Event()

            def _pipe(src: socket.socket, dst: socket.socket) -> None:
                try:
                    while not stop.is_set():
                        data = src.recv(65536)
                        if not data:
                            break
                        dst.sendall(data)
                except OSError as exc:
                    log.debug("proxy pipe error: %s", exc)
                finally:
                    stop.set()
                    with contextlib.suppress(OSError):
                        dst.shutdown(socket.SHUT_WR)

            t1 = threading.Thread(target=_pipe, args=(client_sock, target_sock), daemon=True)
            t2 = threading.Thread(target=_pipe, args=(target_sock, client_sock), daemon=True)
            t1.start()
            t2.start()
            t1.join(timeout=self._cfg.proxy_timeout)
            t2.join(timeout=self._cfg.proxy_timeout)

        except OSError as exc:
            log.debug("proxy connect error: %s", exc)
        finally:
            for s in (client_sock, target_sock):
                if s is not None:
                    with contextlib.suppress(OSError):
                        s.close()

def _wait_for_port(port: int, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with contextlib.suppress(OSError):
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(1)
            s.connect(("127.0.0.1", port))
            s.close()
            return True
        time.sleep(0.5)
    return False

class TtydManager:

    def __init__(self, cfg: ServerConfig, env: EnvironmentManager) -> None:
        self._cfg = cfg
        self._env = env
        self._lock = threading.Lock()
        self._proc: Optional[subprocess.Popen] = None

    def start(self) -> bool:
        if not self._cfg.ttyd_path.exists():
            log.warning("ttyd binary not found; web terminal disabled")
            return False

        cmd = [
            str(self._cfg.ttyd_path.resolve()),
            "--port", str(self._cfg.ttyd_port),
            "--interface", "127.0.0.1",
            "--credential", f"{self._cfg.ssh_user}:{self._cfg.ssh_pass}",
            "--writable",
        ] + self._env.proot_cmd()

        log.info("Starting ttyd on 127.0.0.1:%d", self._cfg.ttyd_port)
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        time.sleep(1)
        if proc.poll() is not None:
            log.error("ttyd died immediately (rc=%d)", proc.returncode)
            return False

        with self._lock:
            self._proc = proc

        if _wait_for_port(self._cfg.ttyd_port, timeout=15):
            log.info("ttyd up on port %d (pid=%d)", self._cfg.ttyd_port, proc.pid)
            return True

        log.warning("ttyd did not open port in time (pid=%d)", proc.pid)
        return True

    def is_alive(self) -> bool:
        with self._lock:
            return self._proc is not None and self._proc.poll() is None

    def stop(self) -> None:
        with self._lock:
            proc = self._proc
            self._proc = None
        if proc is not None and proc.poll() is None:
            proc.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=5)
            if proc.poll() is None:
                proc.kill()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    proc.wait(timeout=3)

    def monitor(self) -> None:
        while True:
            time.sleep(10)
            with self._lock:
                proc = self._proc
            if proc is None:
                continue
            if proc.poll() is not None:
                log.warning("ttyd died (rc=%d), restarting…", proc.returncode)
                with self._lock:
                    self._proc = None
                self.start()

_HTTP_PREFIXES: frozenset[bytes] = frozenset({
    b"GET", b"POS", b"PUT", b"DEL", b"HEA", b"OPT", b"PAT", b"CON",
})

_503_BODY = (
    b"HTTP/1.1 503 Service Unavailable\r\n"
    b"Content-Type: text/html;charset=utf-8\r\n"
    b"Connection: close\r\n\r\n"
    b"<html><body><h1>Terminal Offline</h1>"
    b"<p>The terminal service is not running.</p></body></html>"
)

class _ConnectionToken:

    def __init__(self, sem: threading.Semaphore) -> None:
        self._sem = sem
        self._released = False
        self._lock = threading.Lock()

    def release(self) -> None:
        with self._lock:
            if not self._released:
                self._released = True
                self._sem.release()

    def __enter__(self) -> "_ConnectionToken":
        return self

    def __exit__(self, *_: object) -> None:
        self.release()

class L7Gateway:

    def __init__(
        self,
        cfg: ServerConfig,
        host_key: paramiko.RSAKey,
        env: EnvironmentManager,
        ttyd_mgr: TtydManager,
    ) -> None:
        self._cfg = cfg
        self._host_key = host_key
        self._env = env
        self._ttyd = ttyd_mgr
        self._sem = threading.Semaphore(cfg.max_connections)
        self._ssh = SSHServer(cfg, env)
        self._proxy = TcpProxy(cfg)
        self._shutdown = threading.Event()

    def run(self) -> None:
        server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            server_sock.bind((self._cfg.bind_host, self._cfg.port))
        except OSError as exc:
            log.error("Cannot bind %s:%d — %s", self._cfg.bind_host, self._cfg.port, exc)
            return
        server_sock.listen(20)
        server_sock.settimeout(1.0)
        log.info("Gateway on %s:%d", self._cfg.bind_host, self._cfg.port)

        while not self._shutdown.is_set():
            try:
                client_sock, addr = server_sock.accept()
            except socket.timeout:
                continue
            except OSError:
                if not self._shutdown.is_set():
                    log.exception("accept() error")
                continue

            if not self._sem.acquire(blocking=False):
                log.warning("Too many connections, dropping %s", addr[0])
                with contextlib.suppress(OSError):
                    client_sock.close()
                continue

            tok = _ConnectionToken(self._sem)
            threading.Thread(
                target=self._dispatch,
                args=(client_sock, addr, tok),
                daemon=True,
            ).start()

        server_sock.close()

    def shutdown(self) -> None:
        self._shutdown.set()

    def _dispatch(
        self,
        client_sock: socket.socket,
        addr: tuple[str, int],
        tok: _ConnectionToken,
    ) -> None:
        with tok:
            try:
                self._route(client_sock, addr)
            except Exception:
                log.debug("Dispatch error from %s", addr[0], exc_info=True)
                with contextlib.suppress(OSError):
                    client_sock.close()

    def _peek(self, sock: socket.socket) -> Optional[bytes]:
        readable, _, _ = select.select([sock], [], [], self._cfg.peek_timeout)
        if not readable:
            return b""
        try:
            data = sock.recv(8, socket.MSG_PEEK)
            return data
        except OSError as exc:
            log.debug("peek() error: %s", exc)
            return None

    def _route(self, client_sock: socket.socket, addr: tuple[str, int]) -> None:
        peek = self._peek(client_sock)

        if peek is None:
            with contextlib.suppress(OSError):
                client_sock.close()
            return

        if peek == b"":
            log.debug("SSH (banner wait): %s", addr[0])
            self._handle_ssh(client_sock)
            return

        if not peek:
            with contextlib.suppress(OSError):
                client_sock.close()
            return

        if peek[:4] == b"SSH-":
            log.debug("SSH (banner): %s", addr[0])
            self._handle_ssh(client_sock)
            return

        if peek[:3] in _HTTP_PREFIXES:
            log.debug("HTTP: %s", addr[0])
            self._handle_http(client_sock)
            return

        is_text = all(32 <= b < 127 or b in (9, 10, 13) for b in peek)
        if is_text and self._ttyd.is_alive():
            log.debug("HTTP (heuristic): %s", addr[0])
            self._handle_http(client_sock)
        else:
            log.debug("SSH (fallback): %s", addr[0])
            self._handle_ssh(client_sock)

    def _handle_ssh(self, sock: socket.socket) -> None:
        self._ssh.handle(sock, self._host_key)

    def _handle_http(self, sock: socket.socket) -> None:
        if self._ttyd.is_alive():
            self._proxy.relay(sock, self._cfg.ttyd_port)
        else:
            with contextlib.suppress(OSError):
                sock.sendall(_503_BODY)
            with contextlib.suppress(OSError):
                sock.close()

def bootstrap(cfg: ServerConfig) -> None:
    signal.signal(signal.SIGCHLD, signal.SIG_DFL)

    env = EnvironmentManager(cfg)
    env.setup()
    env.verify_proot()

    if not cfg.key_file.exists():
        log.info("Generating SSH host key…")
        paramiko.RSAKey.generate(2048).write_private_key_file(str(cfg.key_file))
    host_key = paramiko.RSAKey(filename=str(cfg.key_file))
    log.info("SSH key loaded")

    ttyd_mgr = TtydManager(cfg, env)
    ttyd_mgr.start()
    threading.Thread(target=ttyd_mgr.monitor, daemon=True).start()

    gateway = L7Gateway(cfg, host_key, env, ttyd_mgr)

    log.info("=" * 55)
    log.info("  HOST : %s", cfg.advertise_host)
    log.info("  PORT : %d", cfg.port)
    log.info("  Web  : http://%s:%d/", cfg.advertise_host, cfg.port)
    log.info("  SSH  : ssh %s@%s -p %d", cfg.ssh_user, cfg.advertise_host, cfg.port)
    log.info("  Auth : %s / %s", cfg.ssh_user, cfg.ssh_pass)
    log.info("=" * 55)

    def _sig_handler(signum: int, frame: object) -> None:
        log.info("Signal %d received, shutting down…", signum)
        gateway.shutdown()

    signal.signal(signal.SIGINT, _sig_handler)
    signal.signal(signal.SIGTERM, _sig_handler)

    try:
        gateway.run()
    finally:
        ttyd_mgr.stop()
        log.info("Shutdown complete")

def main() -> None:
    cfg = config_from_args()
    bootstrap(cfg)

if __name__ == "__main__":
    main()