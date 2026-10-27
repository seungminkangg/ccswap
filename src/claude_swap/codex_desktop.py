"""Restart the Codex desktop app around a Codex account switch (macOS).

The desktop app (bundle ID ``com.openai.codex``; shipped as ``Codex.app`` and,
in newer releases, ``ChatGPT.app``) runs a bundled ``codex app-server`` that
holds the login in memory and writes refreshed tokens back to
``$CODEX_HOME/auth.json``. Replacing ``auth.json`` underneath it therefore
does nothing until a restart, and a stale writer could even put the old login
back. The handoff here is:

1. find exactly one running desktop instance of this user and refuse when
   another Codex process outside it shares the same ``CODEX_HOME``;
2. wait while any turn started in this app lifetime is still ``inProgress`` in
   Codex's thread history (or the history was written moments ago, or cannot
   be read) -- idle is never assumed;
3. ask the app to quit normally (Apple Event, like Cmd-Q) and wait for it and
   every child it had to exit -- no signals, no kill;
4. switch ``auth.json`` with the regular locked/lineage-checked
   :meth:`CodexAccountSwitcher._switch_auth`;
5. reopen exactly the bundle that was running and confirm the selected login.

Conversations are not resumed or messaged: because only an idle app is
restarted, there is no interrupted turn to continue. A small marker file
records an in-flight handoff (bundle path and slot numbers only, no secrets) so
an interrupted ccswap reopens the app once instead of leaving it closed.
"""

from __future__ import annotations

import json
import os
import plistlib
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from claude_swap.exceptions import SwitchError
from claude_swap.locking import FileLock
from claude_swap.settings import atomic_write_json

if TYPE_CHECKING:
    from claude_swap.codex import CodexAccountSwitcher

BUNDLE_ID = "com.openai.codex"
MARKER_FILENAME = "desktop_handoff.json"
QUIT_TIMEOUT_S = 30.0
LAUNCH_TIMEOUT_S = 30.0
# Thread history written this recently counts as activity even without an
# inProgress row (covers a projection that has not caught up yet).
QUIET_S = 60.0
_POLL_S = 0.5


@dataclass(frozen=True)
class Proc:
    pid: int
    ppid: int
    uid: int
    started: float
    exe: str


@dataclass(frozen=True)
class DesktopApp:
    pid: int
    started: float
    bundle: str
    children: tuple[tuple[int, float], ...] = ()
    server: int | None = None  # bundled ``codex app-server`` child


@dataclass
class DesktopScan:
    apps: list[DesktopApp] = field(default_factory=list)
    others: list[int] = field(default_factory=list)  # outside Codex writers
    issues: list[str] = field(default_factory=list)

    @property
    def app(self) -> DesktopApp | None:
        return self.apps[0] if len(self.apps) == 1 else None

    def problem(self) -> str | None:
        if self.issues:
            return "; ".join(self.issues)
        if len(self.apps) > 1:
            return f"{len(self.apps)} Codex desktop instances are running"
        if self.others:
            pids = ", ".join(map(str, self.others))
            return (f"another Codex process (PID {pids}) shares this CODEX_HOME; "
                    "quit it first so it cannot write the old login back")
        return None


# ---- process inspection --------------------------------------------------

def _ps() -> list[Proc]:
    out = subprocess.run(
        ["ps", "-axww", "-o", "pid=,ppid=,uid=,lstart=,comm="],
        capture_output=True, text=True, check=True, timeout=10,
        env={**os.environ, "LC_ALL": "C"},
    ).stdout
    rows = []
    for line in out.splitlines():
        parts = line.split(None, 8)
        if len(parts) != 9:
            continue
        try:
            started = time.mktime(time.strptime(" ".join(parts[3:8]), "%a %b %d %H:%M:%S %Y"))
            rows.append(Proc(int(parts[0]), int(parts[1]), int(parts[2]), started, parts[8]))
        except ValueError:
            continue
    return rows


_PLIST_CACHE: dict[str, tuple[str, str]] = {}


def _bundle_info(bundle: str) -> tuple[str, str]:
    """(CFBundleIdentifier, CFBundleExecutable) of an .app, "" when unreadable."""
    if bundle not in _PLIST_CACHE:
        try:
            with open(Path(bundle) / "Contents/Info.plist", "rb") as handle:
                info = plistlib.load(handle)
            _PLIST_CACHE[bundle] = (str(info.get("CFBundleIdentifier", "")),
                                    str(info.get("CFBundleExecutable", "")))
        except (OSError, ValueError, plistlib.InvalidFileException):
            return "", ""
    return _PLIST_CACHE[bundle]


def _desktop_bundle(exe: str) -> str | None:
    """Return the top-level .app path when ``exe`` is the desktop's main binary."""
    head, sep, name = exe.rpartition(".app/Contents/MacOS/")
    if not sep or "/" in name or ".app/" in head or not exe.startswith("/"):
        return None
    bundle = head + ".app"
    return bundle if _bundle_info(bundle) == (BUNDLE_ID, name) else None


def _env_codex_home(pid: int) -> tuple[bool, str | None]:
    """(readable, CODEX_HOME value or None) for one process of this user."""
    from claude_swap.process_detection import (
        CTL_KERN, KERN_PROCARGS2, _codex_macos_environment, _codex_macos_sysctl,
    )

    try:
        env, _ = _codex_macos_environment(_codex_macos_sysctl((CTL_KERN, KERN_PROCARGS2, pid)))
    except (OSError, ValueError, RuntimeError):
        return False, None
    for item in env.split(b"\0"):
        if item.startswith(b"CODEX_HOME="):
            return True, os.fsdecode(item[len(b"CODEX_HOME="):])
    return True, None


def _is_codex(proc: Proc) -> bool:
    return Path(proc.exe).name == "codex"


def _same_home(value: str | None, home: Path) -> bool:
    path = Path(os.path.expanduser(value)) if value else Path.home() / ".codex"
    try:
        return path.resolve() == home.resolve()
    except OSError:
        return True  # Unresolvable counts as shared: never assume isolation.


def scan_desktop(home: Path) -> DesktopScan:
    """Find this user's desktop instances and outside Codex writers of ``home``."""
    scan = DesktopScan()
    if sys.platform != "darwin":
        return scan
    try:
        rows = _ps()
    except (OSError, subprocess.SubprocessError) as exc:
        scan.issues.append(f"process list unavailable ({type(exc).__name__})")
        return scan
    uid = os.getuid()
    mine = [p for p in rows if p.uid == uid]
    children: dict[int, list[Proc]] = {}
    for proc in mine:
        children.setdefault(proc.ppid, []).append(proc)
    inside: set[int] = set()
    for proc in mine:
        bundle = _desktop_bundle(proc.exe)
        if bundle is None:
            continue
        stack, tree = [proc.pid], []
        while stack:
            for child in children.get(stack.pop(), []):
                tree.append(child)
                stack.append(child.pid)
        inside.update([proc.pid, *(c.pid for c in tree)])
        server = next((c.pid for c in tree if _is_codex(c)), None)
        scan.apps.append(DesktopApp(proc.pid, proc.started, bundle,
                                    tuple((c.pid, c.started) for c in tree), server))
        readable, value = _env_codex_home(server) if server else (True, None)
        if not readable or not _same_home(value, home):
            scan.issues.append(f"Codex desktop does not use CODEX_HOME {home}")
    for proc in mine:
        if proc.pid in inside or proc.pid == os.getpid() or not _is_codex(proc):
            continue
        readable, value = _env_codex_home(proc.pid)
        if not readable or _same_home(value, home):
            scan.others.append(proc.pid)
    return scan


def _alive(pairs: list[tuple[int, float]]) -> list[int]:
    try:
        current = {(p.pid, p.started) for p in _ps()}
    except (OSError, subprocess.SubprocessError):
        return [pid for pid, _ in pairs]  # Unknown is alive: never assume an exit.
    return [pid for pid, started in pairs if (pid, started) in current]


# ---- activity --------------------------------------------------------------

def desktop_activity(home: Path, since: float, now: float | None = None) -> tuple[str, str]:
    """Return ("idle"|"busy"|"unknown", detail) for turns since app start."""
    now = time.time() if now is None else now

    def version(path: Path) -> int:
        suffix = path.stem.rpartition("_")[2]
        return int(suffix) if suffix.isdecimal() else -1

    dbs = sorted(home.glob("thread_history_*.sqlite"), key=version)
    if not dbs:
        return "unknown", "Codex thread history not found"
    db = dbs[-1]
    try:
        con = sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True, timeout=2)
        try:
            (count,) = con.execute(
                "SELECT COUNT(*) FROM thread_turns WHERE status = 'inProgress' AND started_at >= ?",
                (int(since),),
            ).fetchone()
        finally:
            con.close()
    except sqlite3.Error as exc:
        return "unknown", f"Codex thread history unreadable ({exc.__class__.__name__})"
    if count:
        return "busy", f"{count} Codex turn(s) in progress"
    written = max((p.stat().st_mtime for p in (db, db.with_name(db.name + "-wal")) if p.exists()),
                  default=0.0)
    if now - written < QUIET_S:
        return "busy", f"Codex was active {max(0, int(now - written))}s ago"
    return "idle", ""


# ---- app control -------------------------------------------------------------

def quit_app(app: DesktopApp, timeout: float | None = None) -> str | None:
    """Ask ``app`` to quit normally; return None once it and its children exited."""
    timeout = QUIT_TIMEOUT_S if timeout is None else timeout
    try:
        done = subprocess.run(
            ["osascript", "-e", "on run argv", "-e", "tell application (item 1 of argv) to quit",
             "-e", "end run", app.bundle],
            capture_output=True, text=True, timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"quit request failed ({type(exc).__name__})"
    if done.returncode != 0:
        if "-1743" in done.stderr:
            return ("macOS denied permission to control Codex; allow it under System "
                    "Settings > Privacy & Security > Automation")
        return f"quit request failed ({done.stderr.strip()[:120] or done.returncode})"
    tracked = [(app.pid, app.started), *app.children]
    deadline = time.monotonic() + timeout
    while _alive(tracked):
        if time.monotonic() > deadline:
            return f"Codex desktop did not exit within {timeout:.0f}s (it may be asking to confirm)"
        time.sleep(_POLL_S)
    return None


def launch_app(bundle: str, home: Path, timeout: float | None = None) -> DesktopApp | None:
    """Open ``bundle`` and wait for its main process and bundled app-server."""
    timeout = LAUNCH_TIMEOUT_S if timeout is None else timeout
    try:
        subprocess.run(["open", bundle], capture_output=True, timeout=timeout, check=True)
    except (OSError, subprocess.SubprocessError):
        return None
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        scan = scan_desktop(home)
        app = next((a for a in scan.apps if a.bundle == bundle), None)
        if app is not None and app.server is not None and not scan.issues:
            return app
        time.sleep(_POLL_S)
    return None


# ---- handoff marker ---------------------------------------------------------

def _marker_path(switcher: CodexAccountSwitcher) -> Path:
    return switcher.provider_dir / MARKER_FILENAME


def _write_marker(switcher: CodexAccountSwitcher, **fields: Any) -> None:
    switcher._setup_directories()
    atomic_write_json(_marker_path(switcher), {"schemaVersion": 1, "pid": os.getpid(), **fields})


def _clear_marker(switcher: CodexAccountSwitcher) -> None:
    _marker_path(switcher).unlink(missing_ok=True)


def pending_handoff(switcher: CodexAccountSwitcher) -> dict | None:
    try:
        data = json.loads(_marker_path(switcher).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def recover(switcher: CodexAccountSwitcher) -> str | None:
    """Reopen the desktop once after an interrupted handoff; caller holds the lock."""
    marker = pending_handoff(switcher)
    if marker is None:
        return None
    _clear_marker(switcher)  # One attempt only: never a restart loop.
    bundle = marker.get("bundle")
    # Holding the lock proves the marker's writer died mid-handoff.
    if (not isinstance(bundle, str) or _bundle_info(bundle)[0] != BUNDLE_ID
            or scan_desktop(switcher.codex_home).apps):
        return None
    launch_app(bundle, switcher.codex_home)
    return f"Reopened Codex desktop after an interrupted switch ({bundle})"


def recover_pending(switcher: CodexAccountSwitcher) -> str | None:
    """Run :func:`recover` unless a handoff currently owns the lock."""
    if pending_handoff(switcher) is None:
        return None
    lock = FileLock(switcher.backup_dir / ".codex_desktop.lock", timeout=0)
    if not lock.acquire():
        return None
    try:
        return recover(switcher)
    finally:
        lock.release()


# ---- orchestration -----------------------------------------------------------

def _stay(switcher: CodexAccountSwitcher, reason: str, message: str) -> dict[str, Any]:
    current = switcher.current_account_number()
    ref = {"number": int(current) if current else None, "email": ""}
    return {"switched": False, "from": ref, "to": dict(ref), "reason": reason,
            "message": message, "desktop": {"state": reason, "message": message}}


def status(switcher: CodexAccountSwitcher) -> dict[str, Any]:
    """Read-only report for ``ccswap codex desktop`` and the menu bar."""
    scan = scan_desktop(switcher.codex_home)
    app = scan.app
    activity = desktop_activity(switcher.codex_home, app.started) if app else ("", "")
    return {
        "restartDesktop": switcher.restart_desktop,
        "running": bool(scan.apps),
        "bundle": app.bundle if app else None,
        "pid": app.pid if app else None,
        "activity": activity[0] or None,
        "detail": activity[1] or None,
        "blocked": scan.problem(),
        "otherCodexPids": scan.others,
        "pendingHandoff": pending_handoff(switcher),
    }


def plan(switcher: CodexAccountSwitcher) -> str:
    """Describe, without side effects, what a switch would do to the desktop."""
    report = status(switcher)
    if report["blocked"]:
        return f"would refuse the desktop handoff: {report['blocked']}"
    if not report["running"]:
        return "Codex desktop is not running; it will not be opened"
    if report["activity"] != "idle":
        return f"would wait for Codex desktop to go idle ({report['detail']})"
    return f"would quit and reopen Codex desktop ({report['bundle']})"


def switch_with_desktop(
    switcher: CodexAccountSwitcher, identifier: str, json_output: bool = False,
) -> dict[str, Any]:
    """Switch ``auth.json`` and restart a running, idle Codex desktop around it."""
    lock = FileLock(switcher.backup_dir / ".codex_desktop.lock", timeout=1.0)
    if not lock.acquire():
        return _stay(switcher, "desktop-switch-in-progress",
                     "Another Codex desktop switch is in progress.")
    try:
        recovered = recover(switcher)
        data = switcher._read_sequence()
        number = switcher._resolve(identifier, data)
        if switcher.current_account_number() == number:
            return switcher._switch_auth(identifier, json_output=json_output)
        scan = scan_desktop(switcher.codex_home)
        problem = scan.problem()
        if problem:
            raise SwitchError(f"Codex desktop handoff refused: {problem}; account unchanged")
        app = scan.app
        if app is None:
            result = switcher._switch_auth(identifier, json_output=json_output)
            result["desktop"] = {"state": "not-running", "message": (
                "Codex desktop is not running; the account applies when Codex starts.")}
            return result
        state, detail = desktop_activity(switcher.codex_home, app.started)
        if state != "idle":
            message = f"Waiting for Codex desktop to go idle ({detail}); account unchanged."
            if not json_output:
                print(message)
            return _stay(switcher, "desktop-busy", message)

        current = switcher.current_account_number()
        _write_marker(switcher, phase="quitting", bundle=app.bundle, to=int(number),
                      **{"from": int(current) if current else None}, startedAt=time.time())
        failure = quit_app(app)
        if failure is None:
            after = scan_desktop(switcher.codex_home)
            failure = ("Codex desktop reopened during the switch" if after.apps
                       else after.problem())
        if failure:
            _clear_marker(switcher)
            raise SwitchError(f"Codex desktop handoff stopped: {failure}; account unchanged")

        _write_marker(switcher, phase="switching", bundle=app.bundle, to=int(number),
                      startedAt=time.time())
        try:
            result = switcher._switch_auth(identifier, json_output=True)
        except Exception:
            launch_app(app.bundle, switcher.codex_home)  # Back on the unchanged login.
            _clear_marker(switcher)
            raise
        _write_marker(switcher, phase="launching", bundle=app.bundle, to=int(number),
                      startedAt=time.time())
        relaunched = launch_app(app.bundle, switcher.codex_home)
        _clear_marker(switcher)
        active = switcher.current_account_number()
        if active != number:
            message = (f"Codex login is Account {active}, not {number}, after the restart; "
                       "check 'ccswap codex status'")
            state = "verify-failed"
        elif relaunched is None:
            message = f"Switched, but Codex desktop did not reopen; open {app.bundle} manually."
            state = "relaunch-failed"
        else:
            message = f"Codex desktop restarted on Account {number}."
            state = "restarted"
        result["desktop"] = {"state": state, "message": message, "bundle": app.bundle,
                             **({"recovered": recovered} if recovered else {})}
        if not json_output:
            print(f"Switched to Codex Account {number}: {result['to']['email']}")
            print(message)
        if state == "verify-failed":
            raise SwitchError(message)
        return result
    finally:
        lock.release()
