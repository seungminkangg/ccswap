"""Restart the Codex desktop app around a Codex account switch (macOS).

The desktop app (bundle ID ``com.openai.codex``; shipped as ``Codex.app`` and,
in newer releases, ``ChatGPT.app``) runs a bundled ``codex app-server`` that
holds the login in memory and writes refreshed tokens back to
``$CODEX_HOME/auth.json``. Replacing ``auth.json`` underneath it therefore
does nothing until a restart, and a stale writer could even put the old login
back. The handoff here is:

1. find exactly one running desktop instance of this user, and wait while
   another Codex process outside it shares the same ``CODEX_HOME``;
2. wait while any turn started in this app lifetime is still ``inProgress`` in
   Codex's thread history (or the history was written moments ago, or cannot
   be read) -- idle is never assumed;
3. ask the app to quit normally (Apple Event, like Cmd-Q) and wait for it and
   every child it had to exit -- no signals, no kill;
4. switch ``auth.json`` with the regular locked/lineage-checked
   :meth:`CodexAccountSwitcher._switch_auth`;
5. reopen exactly the bundle that was running and confirm the selected login.

Only an idle app is restarted, so no running turn is cut off. Threads whose
last turn the usage limit had already stopped are continued afterwards only with
``codex.resumeAfterRestart``: each such turn gets one ``continue`` through the
bundled ``codex exec resume`` (the desktop's own app-server takes no outside
clients), recorded first so it is never sent twice. A small marker file
records an in-flight handoff (bundle path and slot numbers only, no secrets) so
an interrupted ccswap reopens the app once instead of leaving it closed.
"""

from __future__ import annotations

import json
import logging
import os
import plistlib
import shutil
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

_logger = logging.getLogger("claude-swap")

BUNDLE_ID = "com.openai.codex"
MARKER_FILENAME = "desktop_handoff.json"
QUIT_TIMEOUT_S = 30.0
LAUNCH_TIMEOUT_S = 30.0
# Thread history written this recently counts as activity even without an
# inProgress row (covers a projection that has not caught up yet).
QUIET_S = 60.0
_POLL_S = 0.5
RESUME_PROMPT = "continue"
RESUME_WINDOW_S = 6 * 3600.0  # Older limit stops are left to the user.
RESUME_MAX = 5  # Threads per restart; each one spends the new account's quota.
RESUME_LEDGER = "resumed_turns.json"
_LEDGER_KEEP_S = 7 * 86400.0


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
    # Descendant ``codex`` processes (app-servers): the ones that write auth.json.
    writers: tuple[tuple[int, float], ...] = ()

    @property
    def server(self) -> int | None:
        return self.writers[0][0] if self.writers else None


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
            # ponytail: local-time lstart is ambiguous in a DST fall-back hour; read
            # p_starttime via sysctl if that ever matters.
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
        app = DesktopApp(proc.pid, proc.started, bundle,
                         tuple((c.pid, c.started) for c in tree if _is_codex(c)))
        scan.apps.append(app)
        readable, value = _env_codex_home(app.server) if app.server else (True, None)
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

def _latest_db(home: Path, stem: str) -> Path | None:
    """Newest ``<stem>_<N>.sqlite`` in ``home`` (Codex bumps N on migrations)."""
    def version(path: Path) -> int:
        suffix = path.stem.rpartition("_")[2]
        return int(suffix) if suffix.isdecimal() else -1

    dbs = sorted(home.glob(f"{stem}_*.sqlite"), key=version)
    return dbs[-1] if dbs else None


def desktop_activity(home: Path, since: float, now: float | None = None) -> tuple[str, str]:
    """Return ("idle"|"busy"|"unknown", detail) for turns since app start."""
    now = time.time() if now is None else now
    db = _latest_db(home, "thread_history")
    if db is None:
        return "unknown", "Codex thread history not found"
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
    written = 0.0
    for path in (db, db.with_name(db.name + "-wal")):
        try:
            written = max(written, path.stat().st_mtime)
        except FileNotFoundError:
            continue
        except OSError as exc:
            return "unknown", f"Codex thread history unreadable ({exc.__class__.__name__})"
    if now - written < QUIET_S:
        return "busy", f"Codex was active {max(0, int(now - written))}s ago"
    return "idle", ""


# ---- app control -------------------------------------------------------------

def _gone(app: DesktopApp) -> bool:
    return not _alive([(app.pid, app.started)])


def quit_app(app: DesktopApp, timeout: float | None = None) -> str | None:
    """Ask ``app`` to quit normally; None once it and its app-servers exited.

    Other descendants (MCP servers, background jobs) may outlive the app; they
    cannot write ``auth.json``, so they are not waited for.
    """
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
    tracked = [(app.pid, app.started), *app.writers]
    deadline = time.monotonic() + timeout
    while _alive(tracked):
        if time.monotonic() > deadline:
            return f"Codex desktop did not exit within {timeout:.0f}s (it may be asking to confirm)"
        time.sleep(_POLL_S)
    return None


def launch_app(bundle: str, home: Path, timeout: float | None = None) -> DesktopApp | None:
    """Open ``bundle``; return the instance it started once its app-server runs.

    Only an instance absent before ``open`` counts, so an app someone reopened
    on the old login is never mistaken for the relaunch.
    """
    timeout = LAUNCH_TIMEOUT_S if timeout is None else timeout
    before = {(a.pid, a.started) for a in scan_desktop(home).apps}
    try:
        subprocess.run(["open", bundle], capture_output=True, timeout=timeout, check=True)
    except (OSError, subprocess.SubprocessError):
        return None
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        scan = scan_desktop(home)
        app = next((a for a in scan.apps if a.bundle == bundle
                    and (a.pid, a.started) not in before), None)
        if app is not None and app.server is not None and not scan.issues:
            return app
        time.sleep(_POLL_S)
    return None


# ---- resume ------------------------------------------------------------------

def _query(db: Path, sql: str, args: tuple) -> list[tuple]:
    con = sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True, timeout=2)
    try:
        return con.execute(sql, args).fetchall()
    finally:
        con.close()


def stopped_threads(home: Path, now: float | None = None) -> list[tuple[str, str, str, list[str]]]:
    """(thread, turn, cwd, model args) whose latest turn hit the usage limit recently.

    Archived and subagent threads are left alone; a subagent is continued by
    its parent. The thread's own model and effort are kept, not config.toml's.
    """
    now = time.time() if now is None else now
    history, state = _latest_db(home, "thread_history"), _latest_db(home, "state")
    if history is None or state is None:
        return []
    turns = _query(history, """
        SELECT t.thread_id, t.turn_id FROM thread_turns t
        WHERE t.status = 'failed'
          AND json_extract(t.error_json, '$.codexErrorInfo') = 'usageLimitExceeded'
          AND t.completed_at >= ?
          AND t.rollout_ordinal = (SELECT MAX(rollout_ordinal) FROM thread_turns
                                   WHERE thread_id = t.thread_id)
        ORDER BY t.completed_at DESC""", (int(now - RESUME_WINDOW_S),))
    found = []
    for thread, turn in turns:
        row = _query(state, """
            SELECT cwd, model, reasoning_effort FROM threads
            WHERE id = ? AND archived = 0 AND source NOT LIKE '%"subagent"%'
              AND COALESCE(thread_source, '') != 'agent_created_thread'""", (thread,))
        if row:
            cwd, model, effort = row[0]
            args = [*(["-m", model] if model else []),
                    *(["-c", f"model_reasoning_effort={effort}"] if effort else [])]
            found.append((thread, turn, cwd, args))
    return found


def _bundled_cli(bundle: str) -> str | None:
    """The desktop's own CLI (same version as the app-server), else PATH codex."""
    cli = Path(bundle) / "Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex"
    return str(cli) if cli.is_file() else shutil.which("codex")


def resume_stopped(switcher: CodexAccountSwitcher, bundle: str) -> list[str]:
    """Send one ``continue`` to each limit-stopped thread; return thread ids."""
    ledger_path = switcher.provider_dir / RESUME_LEDGER
    try:
        ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        ledger = {}
    now = time.time()
    ledger = {turn: at for turn, at in (ledger.items() if isinstance(ledger, dict) else ())
              if isinstance(at, (int, float)) and now - at < _LEDGER_KEEP_S}
    todo = [t for t in stopped_threads(switcher.codex_home, now) if t[1] not in ledger]
    cli = _bundled_cli(bundle)
    if not todo or cli is None:
        return []
    started = []
    for thread, turn, cwd, model_args in todo[:RESUME_MAX]:
        ledger[turn] = now
        atomic_write_json(ledger_path, ledger)  # Recorded before sending: at most once.
        try:
            subprocess.Popen(
                [cli, "exec", "--skip-git-repo-check", "resume", *model_args,
                 thread, RESUME_PROMPT],
                cwd=cwd if os.path.isdir(cwd) else str(Path.home()),
                env={**os.environ, "CODEX_HOME": str(switcher.codex_home)},
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, start_new_session=True,
            )
        except OSError:
            continue
        started.append(thread)
    return started


# ---- handoff marker ---------------------------------------------------------

# Phases in which ccswap had confirmed the app closed; only these are recovered.
_CLOSED_PHASES = ("closed", "switching", "launching")
MARKER_MAX_AGE_S = 3600.0


def _marker_path(switcher: CodexAccountSwitcher) -> Path:
    return switcher.provider_dir / MARKER_FILENAME


def _write_marker(switcher: CodexAccountSwitcher, **fields: Any) -> None:
    switcher._setup_directories()
    atomic_write_json(_marker_path(switcher), {
        "schemaVersion": 1, "pid": os.getpid(), "startedAt": time.time(), **fields})


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
    bundle, started = marker.get("bundle"), marker.get("startedAt")
    # Holding the lock proves the marker's writer died mid-handoff. A stale
    # marker, or one from before the app was confirmed closed, reopens nothing.
    if (marker.get("phase") not in _CLOSED_PHASES or not isinstance(bundle, str)
            or not isinstance(started, (int, float))
            or time.time() - started > MARKER_MAX_AGE_S
            or _bundle_info(bundle)[0] != BUNDLE_ID
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

def _stay(switcher: CodexAccountSwitcher, reason: str, message: str,
          json_output: bool) -> dict[str, Any]:
    if not json_output:
        print(message)
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
        "blocked": scan.problem() if scan.apps or scan.issues else None,
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
    from claude_swap.codex import _codex_restart_hint

    lock = FileLock(switcher.backup_dir / ".codex_desktop.lock", timeout=1.0)
    if not lock.acquire():
        return _stay(switcher, "desktop-switch-in-progress",
                     "Another Codex desktop switch is in progress.", json_output)
    try:
        recovered = recover(switcher)
        data = switcher._read_sequence()
        number = switcher._resolve(identifier, data)
        if switcher.current_account_number() == number:
            return switcher._switch_auth(identifier, json_output=json_output)
        scan = scan_desktop(switcher.codex_home)
        if scan.issues:
            raise SwitchError(f"Codex desktop handoff refused: {scan.problem()}; account unchanged")
        if not scan.apps:
            result = switcher._switch_auth(identifier, json_output=json_output)
            result["desktop"] = {"state": "not-running", "message": _codex_restart_hint()}
            return result
        if len(scan.apps) == 1 and scan.others:
            # A CLI run (or a thread we resumed) is transient: wait, not an error.
            return _stay(switcher, "desktop-busy",
                         f"Waiting for {scan.problem()}; account unchanged.", json_output)
        problem = scan.problem()
        if problem:
            raise SwitchError(f"Codex desktop handoff refused: {problem}; account unchanged")
        app = scan.app
        state, detail = desktop_activity(switcher.codex_home, app.started)
        if state != "idle":
            return _stay(switcher, "desktop-busy",
                         f"Waiting for Codex desktop to go idle ({detail}); account unchanged.",
                         json_output)
        # Everything _switch_auth could refuse, checked before costing a restart.
        switcher._switch_preflight(identifier)

        failure = quit_app(app)
        if failure is None:
            try:
                _write_marker(switcher, phase="closed", bundle=app.bundle, to=int(number))
            except OSError as exc:
                failure = f"could not record the handoff ({exc.__class__.__name__})"
        if failure is None:
            after = scan_desktop(switcher.codex_home)  # Immediately before the write.
            failure = ("Codex desktop reopened during the switch" if after.apps
                       else after.problem())
        if failure:
            _clear_marker(switcher)
            if _gone(app) and not scan_desktop(switcher.codex_home).apps:
                launch_app(app.bundle, switcher.codex_home)  # Unchanged login.
            raise SwitchError(f"Codex desktop handoff stopped: {failure}; account unchanged")

        try:
            _write_marker(switcher, phase="switching", bundle=app.bundle, to=int(number))
            result = switcher._switch_auth(identifier, json_output=True)
        except Exception:
            launch_app(app.bundle, switcher.codex_home)  # Back on the unchanged login.
            _clear_marker(switcher)
            raise
        try:
            _write_marker(switcher, phase="launching", bundle=app.bundle, to=int(number))
        except OSError:
            pass  # The switch is done; a missing marker only skips crash recovery.
        relaunched = launch_app(app.bundle, switcher.codex_home)
        _clear_marker(switcher)
        active = switcher.current_account_number()
        running = scan_desktop(switcher.codex_home).apps
        if active != number:
            message = (f"Codex login is Account {active}, not {number}, after the restart; "
                       "check 'ccswap codex status'")
            state = "verify-failed"
        elif relaunched is not None and len(running) > 1:
            message = ("A Codex desktop instance from before the switch is still running on "
                       "the old login; quit Codex and open it again.")
            state = "verify-failed"
        elif relaunched is None:
            message = (f"Switched, but Codex desktop did not reopen; open {app.bundle} manually."
                       if not running else
                       "Switched, but a Codex desktop instance opened before the switch "
                       "finished may hold the old login; quit Codex and open it again.")
            state = "relaunch-failed"
        else:
            message = f"Codex desktop restarted on Account {number}."
            state = "restarted"
        resumed: list[str] = []
        if state == "restarted" and switcher.resume_after_restart:
            try:
                resumed = resume_stopped(switcher, app.bundle)
            except (OSError, sqlite3.Error):
                _logger.warning("resuming stopped Codex threads failed", exc_info=True)
            if resumed:
                message += (f" Continuing {len(resumed)} thread(s) the usage limit stopped; "
                            "let them finish before typing in them.")
        result["desktop"] = {"state": state, "message": message, "bundle": app.bundle,
                             **({"recovered": recovered} if recovered else {}),
                             **({"resumed": resumed} if resumed else {})}
        if not json_output:
            print(f"Switched to Codex Account {number}: {result['to']['email']}")
            print(message)
        return result
    finally:
        lock.release()
