"""AIMurahV3 command-line interface.

Supports:
  aimurahv3 start [--foreground] [--proxy-port N] [--dashboard-port N]
  aimurahv3 stop
  aimurahv3 restart
  aimurahv3 status
  aimurahv3 version
  aimurahv3 logs [-n N] [-f]
  aimurahv3 set-password
  aimurahv3 apikey [--rotate]
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import getpass
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from . import __version__
from .config import (
    LOG_PATH,
    PID_PATH,
    ensure_data_dir,
    load_config,
    rotate_api_key,
    save_config,
    set_dashboard_password,
)

WINDOWS = os.name == "nt"


# ---------------- Daemon lifecycle helpers ----------------

def _read_pid() -> int | None:
    if not PID_PATH.exists():
        return None
    try:
        return int(PID_PATH.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if WINDOWS:
        try:
            out = subprocess.check_output(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
                stderr=subprocess.DEVNULL, text=True,
            )
            return str(pid) in out
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _write_pid(pid: int) -> None:
    ensure_data_dir()
    PID_PATH.write_text(str(pid), encoding="utf-8")


def _clear_pid() -> None:
    with contextlib.suppress(OSError):
        PID_PATH.unlink()


def _get_active_pid() -> int | None:
    pid = _read_pid()
    if pid and _pid_alive(pid):
        return pid
    if pid:
        _clear_pid()
    return None


def _port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.25):
            return True
    except OSError:
        return False


def _prepare_startup_log() -> None:
    if not LOG_PATH.exists():
        return
    previous_log = LOG_PATH.with_suffix(LOG_PATH.suffix + ".prev")
    with contextlib.suppress(OSError):
        previous_log.unlink()
    with contextlib.suppress(OSError):
        LOG_PATH.replace(previous_log)


# ---------------- `start` ----------------

def cmd_start(args: argparse.Namespace) -> int:
    cfg = load_config()
    changed = False
    if args.proxy_port:
        cfg["proxy_port"] = int(args.proxy_port)
        changed = True
    if args.dashboard_port:
        cfg["dashboard_port"] = int(args.dashboard_port)
        changed = True
    if args.host:
        cfg["proxy_host"] = args.host
        cfg["dashboard_host"] = args.host
        changed = True
    if changed:
        save_config(cfg)

    pid = _get_active_pid()
    if pid:
        print(f"AIMurahV3 already running (pid={pid}).")
        print(f"  Proxy:     http://{cfg['proxy_host']}:{cfg['proxy_port']}")
        print(f"  Dashboard: http://{cfg['dashboard_host']}:{cfg['dashboard_port']}")
        return 1

    if args.foreground:
        from .daemon import run_foreground
        _prepare_startup_log()
        _write_pid(os.getpid())
        try:
            return run_foreground()
        finally:
            _clear_pid()

    return _spawn_background(cfg)


def _spawn_background(cfg: dict[str, Any]) -> int:
    """Launch the daemon detached from the calling shell."""
    python_exe = sys.executable
    module_args = [python_exe, "-m", "aimurah", "start", "--foreground"]
    ensure_data_dir()
    _prepare_startup_log()

    log_handle = open(LOG_PATH, "a", encoding="utf-8")
    creationflags = 0
    start_new_session = False
    if WINDOWS:
        creationflags = 0x00000200 | 0x00000008  # CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS
    else:
        start_new_session = True

    proc = subprocess.Popen(
        module_args,
        stdout=log_handle, stderr=log_handle, stdin=subprocess.DEVNULL,
        creationflags=creationflags,
        start_new_session=start_new_session,
        close_fds=True,
    )

    proxy_host = str(cfg["proxy_host"])
    proxy_port = int(cfg["proxy_port"])
    dash_host = str(cfg["dashboard_host"])
    dash_port = int(cfg["dashboard_port"])

    # Wait until both services are listening before declaring success.
    for _ in range(80):
        time.sleep(0.25)
        if proc.poll() is not None:
            print(f"AIMurahV3 exited during startup (code {proc.returncode}). See {LOG_PATH}")
            return proc.returncode or 1
        if _pid_alive(proc.pid) and _port_open(proxy_host, proxy_port) and _port_open(dash_host, dash_port):
            _write_pid(proc.pid)
            break
    else:
        print(f"AIMurahV3 did not finish startup. See {LOG_PATH}")
        return 1

    print(f"AIMurahV3 {__version__} started (pid={proc.pid})")
    print(f"  Proxy:     http://{cfg['proxy_host']}:{cfg['proxy_port']}")
    print(f"  Dashboard: http://{cfg['dashboard_host']}:{cfg['dashboard_port']}")
    print(f"  Log file:  {LOG_PATH}")
    return 0


# ---------------- `stop` ----------------

def cmd_stop(_args: argparse.Namespace) -> int:
    pid = _get_active_pid()
    if not pid:
        print("AIMurahV3 is not running.")
        return 0

    print(f"Stopping AIMurahV3 (pid={pid})...")
    try:
        if WINDOWS:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        else:
            os.kill(pid, signal.SIGTERM)
    except Exception as exc:  # noqa: BLE001
        print(f"stop failed: {exc}")
        return 1

    for _ in range(40):
        if not _pid_alive(pid):
            break
        time.sleep(0.25)

    if _pid_alive(pid) and not WINDOWS:
        os.kill(pid, signal.SIGKILL)

    _clear_pid()
    print("Stopped.")
    return 0


# ---------------- `restart` ----------------

def cmd_restart(args: argparse.Namespace) -> int:
    cmd_stop(args)
    time.sleep(0.5)
    return cmd_start(args)


# ---------------- `status` ----------------

def cmd_status(_args: argparse.Namespace) -> int:
    cfg = load_config()
    pid = _get_active_pid()
    print(f"AIMurahV3 {__version__}")
    if pid:
        print(f"  Status:    running (pid={pid})")
    else:
        print("  Status:    stopped")
    print(f"  Proxy:     http://{cfg['proxy_host']}:{cfg['proxy_port']}")
    print(f"  Dashboard: http://{cfg['dashboard_host']}:{cfg['dashboard_port']}")
    print(f"  Data dir:  {LOG_PATH.parent}")
    return 0 if pid else 3


# ---------------- `version` ----------------

def cmd_version(_args: argparse.Namespace) -> int:
    print(f"AIMurahV3 {__version__}")
    return 0


# ---------------- `logs` ----------------

def cmd_logs(args: argparse.Namespace) -> int:
    if not LOG_PATH.exists():
        print(f"No log file at {LOG_PATH}")
        return 1
    lines = Path(LOG_PATH).read_text(encoding="utf-8", errors="replace").splitlines()
    tail = lines[-args.lines:] if args.lines else lines
    for line in tail:
        print(line)
    if args.follow:
        try:
            with Path(LOG_PATH).open("r", encoding="utf-8", errors="replace") as fh:
                fh.seek(0, os.SEEK_END)
                while True:
                    chunk = fh.readline()
                    if not chunk:
                        time.sleep(0.4)
                        continue
                    sys.stdout.write(chunk)
                    sys.stdout.flush()
        except KeyboardInterrupt:
            return 0
    return 0


# ---------------- `set-password` ----------------

def cmd_set_password(args: argparse.Namespace) -> int:
    pwd = args.password or getpass.getpass("New dashboard password: ")
    if not pwd or len(pwd) < 6:
        print("Password must be at least 6 characters.")
        return 1
    confirm = args.password or getpass.getpass("Confirm: ")
    if pwd != confirm:
        print("Passwords do not match.")
        return 1
    set_dashboard_password(pwd)
    print("Dashboard password updated.")
    return 0


# ---------------- `apikey` ----------------

def cmd_apikey(args: argparse.Namespace) -> int:
    cfg = load_config()
    if args.rotate:
        key = rotate_api_key()
        print(f"Rotated. New key: {key}")
    else:
        print(cfg.get("api_key", ""))
    return 0


# ---------------- argparse wiring ----------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="aimurahv3", description="AIMurahV3 — Kiro Pro proxy gateway")
    p.add_argument("--version", action="store_true", help="Print version and exit")
    sub = p.add_subparsers(dest="command")

    s = sub.add_parser("start", help="Start the daemon (default: background)")
    s.add_argument("--foreground", action="store_true", help="Run in the foreground (blocking)")
    s.add_argument("--proxy-port", type=int, help="Override proxy port")
    s.add_argument("--dashboard-port", type=int, help="Override dashboard port")
    s.add_argument("--host", help="Override bind host for proxy + dashboard")
    s.set_defaults(func=cmd_start)

    s = sub.add_parser("stop", help="Stop the running daemon")
    s.set_defaults(func=cmd_stop)

    s = sub.add_parser("restart", help="Restart the daemon")
    s.add_argument("--foreground", action="store_true")
    s.add_argument("--proxy-port", type=int)
    s.add_argument("--dashboard-port", type=int)
    s.add_argument("--host")
    s.set_defaults(func=cmd_restart)

    s = sub.add_parser("status", help="Show daemon status")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("version", help="Print version")
    s.set_defaults(func=cmd_version)

    s = sub.add_parser("logs", help="Show log lines")
    s.add_argument("-n", "--lines", type=int, default=100, help="Number of lines to show")
    s.add_argument("-f", "--follow", action="store_true", help="Follow the log")
    s.set_defaults(func=cmd_logs)

    s = sub.add_parser("set-password", help="Set the dashboard password")
    s.add_argument("--password", help="Provide password inline (not recommended)")
    s.set_defaults(func=cmd_set_password)

    s = sub.add_parser("apikey", help="Show or rotate the proxy API key")
    s.add_argument("--rotate", action="store_true", help="Generate a new API key")
    s.set_defaults(func=cmd_apikey)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if getattr(args, "version", False):
        return cmd_version(args)
    if not getattr(args, "command", None):
        parser.print_help()
        return 0
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
