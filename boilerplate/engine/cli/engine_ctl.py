#!/usr/bin/env python3
"""
cli/engine_ctl.py — start / stop / status the asyncio daemon (tgengine.engine).

The daemon is a long-lived process that scans the SQLite control-plane for due work
(warmup, parse, DM, reply, score) and acts on it. There is no HTTP API and no init
system in the pod, so this CLI is the process supervisor: it spawns the daemon
detached (own session via setsid/start_new_session), tracks it with a pidfile under
$TGENGINE_HOME (or the cwd), and tails its log for a status readout.

State it manages, all under $TGENGINE_HOME (fallback: cwd):
  engine.pid   — the daemon's pid (removed on --stop)
  engine.log   — combined stdout+stderr of the daemon

TGENGINE_DB / TGENGINE_HOME are passed through to the child so it binds the same DB.
Uses only os / subprocess / signal — no extra deps. Prints JSON; never prints secrets.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time

# Make the package importable when run as `python cli/engine_ctl.py` from repo root.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# db.py binds its SQLite engine to TGENGINE_DB at import time, so honor --db BEFORE
# importing the package (a tiny pre-scan; argparse still owns real validation below).
_argv = sys.argv[1:]
for _i, _a in enumerate(_argv):
    if _a == "--db" and _i + 1 < len(_argv):
        os.environ["TGENGINE_DB"] = _argv[_i + 1]
    elif _a.startswith("--db="):
        os.environ["TGENGINE_DB"] = _a[len("--db="):]


def _home() -> str:
    return os.environ.get("TGENGINE_HOME", ".")


def _pidfile() -> str:
    return os.path.join(_home(), "engine.pid")


def _logfile() -> str:
    return os.path.join(_home(), "engine.log")


def _pid_alive(pid: int) -> bool:
    """True if a process with this pid exists (signal 0 probes without killing)."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Exists but owned by another user — still counts as alive.
        return True
    return True


def _read_pid() -> int | None:
    path = _pidfile()
    try:
        with open(path) as fh:
            raw = fh.read().strip()
    except FileNotFoundError:
        return None
    if not raw.isdigit():
        return None
    return int(raw)


def _tail(path: str, n: int = 20) -> list[str]:
    try:
        with open(path, "r", errors="replace") as fh:
            lines = fh.read().splitlines()
    except FileNotFoundError:
        return []
    return lines[-n:]


def do_start() -> dict:
    existing = _read_pid()
    if existing is not None and _pid_alive(existing):
        return {"ok": False, "action": "start", "running": True, "pid": existing,
                "error": "engine already running"}

    log_path = _logfile()
    os.makedirs(_home(), exist_ok=True)
    log_fh = open(log_path, "a")
    env = os.environ.copy()  # carries TGENGINE_DB / TGENGINE_HOME to the child

    proc = subprocess.Popen(
        [sys.executable, "-m", "tgengine.engine"],
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True,  # detach: own session/process group, survives this CLI
        env=env,
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    )
    log_fh.close()

    with open(_pidfile(), "w") as fh:
        fh.write(str(proc.pid))

    # Brief settle so an instant crash is reported instead of a false "running".
    time.sleep(0.3)
    alive = _pid_alive(proc.pid)
    result = {"ok": alive, "action": "start", "running": alive, "pid": proc.pid,
              "log": log_path}
    if not alive:
        result["error"] = "engine exited immediately"
        result["log_tail"] = _tail(log_path)
    return result


def do_stop() -> dict:
    pid = _read_pid()
    if pid is None:
        return {"ok": False, "action": "stop", "running": False,
                "error": "no pidfile"}

    if not _pid_alive(pid):
        # Stale pidfile — clean it up and report not-running.
        try:
            os.remove(_pidfile())
        except FileNotFoundError:
            pass
        return {"ok": True, "action": "stop", "running": False, "pid": pid,
                "note": "stale pidfile removed"}

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass

    # Wait for graceful exit, then escalate to SIGKILL.
    for _ in range(50):  # up to ~5s
        if not _pid_alive(pid):
            break
        time.sleep(0.1)
    killed = False
    if _pid_alive(pid):
        try:
            os.kill(pid, signal.SIGKILL)
            killed = True
        except ProcessLookupError:
            pass
        for _ in range(20):
            if not _pid_alive(pid):
                break
            time.sleep(0.1)

    try:
        os.remove(_pidfile())
    except FileNotFoundError:
        pass

    stopped = not _pid_alive(pid)
    result = {"ok": stopped, "action": "stop", "running": not stopped, "pid": pid,
              "sigkill": killed}
    if not stopped:
        result["error"] = "process did not terminate"
    return result


def do_status() -> dict:
    pid = _read_pid()
    running = pid is not None and _pid_alive(pid)
    return {
        "ok": True,
        "action": "status",
        "running": running,
        "pid": pid if running else None,
        "pidfile": _pidfile(),
        "log": _logfile(),
        "log_tail": _tail(_logfile()),
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Start/stop/status the tgengine asyncio daemon in the pod."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--start", action="store_true",
                       help="spawn 'python -m tgengine.engine' detached (refuse if already running)")
    group.add_argument("--stop", action="store_true",
                       help="terminate the daemon from the pidfile and remove it")
    group.add_argument("--status", action="store_true",
                       help="report running/pid and the last log lines")
    parser.add_argument("--db", help="path to the SQLite DB (sets TGENGINE_DB; "
                                     "applied at startup before the DB engine binds)")
    args = parser.parse_args()

    if args.start:
        result = do_start()
    elif args.stop:
        result = do_stop()
    else:
        result = do_status()

    print(json.dumps(result))
    if not result.get("ok"):
        sys.exit(1)


if __name__ == "__main__":
    main()
