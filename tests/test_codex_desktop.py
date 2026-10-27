"""Tests for restarting the Codex desktop app around a Codex switch.

Everything macOS-specific is faked: the process table, ``osascript`` quits,
``open`` launches, process environments and Codex's thread-history database.
Accounts are synthetic and go through the real locked ``_switch_auth``.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time

import pytest

from claude_swap import codex, codex_desktop
from claude_swap.autoswitch import TickOutcome
from claude_swap.codex_autoswitch import STATE_FILENAME, CodexAutoSwitchEngine
from claude_swap.codex_desktop import Proc
from claude_swap.exceptions import SwitchError
from claude_swap.settings import AutoSwitchSettings, set_setting
from tests.test_codex import _auth

BUNDLE = "/Applications/ChatGPT.app"
MAIN = f"{BUNDLE}/Contents/MacOS/ChatGPT"
SERVER = f"{BUNDLE}/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex"
NOW = time.time()


class FakeMac:
    """Process table plus the two commands the handoff runs."""

    def __init__(self) -> None:
        self.procs: list[Proc] = []
        self.env: dict[int, str | None] = {}
        self.calls: list[list[str]] = []
        self.quit_mode = "ok"  # ok | refuse | denied | hang | reopen
        self.launch_ok = True
        self.run_override = None
        self._next = 1000

    def start_app(self, bundle: str = BUNDLE, started: float = NOW - 3600) -> int:
        main = self._spawn(1, f"{bundle}/Contents/MacOS/ChatGPT", started)
        self._spawn(main, f"{bundle}/Contents/Frameworks/X.framework/Helpers/"
                          "Codex (Renderer).app/Contents/MacOS/Codex (Renderer)", started)
        server = self._spawn(main, SERVER.replace(BUNDLE, bundle), started)
        self._spawn(server, "/bin/zsh", started)
        return main

    def _spawn(self, ppid: int, exe: str, started: float = NOW) -> int:
        self._next += 1
        self.procs.append(Proc(self._next, ppid, os.getuid(), started, exe))
        return self._next

    def spawn_cli(self, codex_home: str | None = None) -> int:
        pid = self._spawn(1, "/opt/homebrew/bin/codex")
        self.env[pid] = codex_home
        return pid

    def quit_leaving(self, keep) -> None:
        """Make the next quit exit everything except processes matching ``keep``."""
        def run(argv, **kw):
            if argv[0] != "osascript":
                return FakeMac.run(self, argv, **kw)
            self.calls.append(list(argv))
            self.procs = [p for p in self.procs if keep(p)]
            return subprocess.CompletedProcess(argv, 0, "", "")
        self.run_override = run

    def apps(self) -> list[int]:
        return [p.pid for p in self.procs if p.exe.endswith(".app/Contents/MacOS/ChatGPT")]

    def run(self, argv, **_kw):
        self.calls.append(list(argv))
        if argv[0] == "osascript":
            if self.quit_mode == "refuse":
                return subprocess.CompletedProcess(argv, 1, "", "execution error (-128)")
            if self.quit_mode == "denied":
                return subprocess.CompletedProcess(argv, 1, "", "Not authorized (-1743)")
            if self.quit_mode in ("ok", "reopen"):
                self.procs = []
                if self.quit_mode == "reopen":
                    self.start_app()
            return subprocess.CompletedProcess(argv, 0, "", "")
        if argv[0] == "pgrep":  # _codex_restart_hint's liveness probe
            return subprocess.CompletedProcess(argv, 1, b"", b"")
        if argv[0] == "open":
            if not self.launch_ok:
                raise subprocess.CalledProcessError(1, argv)
            self.start_app(argv[1], started=time.time())
            return subprocess.CompletedProcess(argv, 0, b"", b"")
        raise AssertionError(f"unexpected command {argv}")

    def commands(self) -> list[str]:
        return [call[0] for call in self.calls]


@pytest.fixture
def mac(monkeypatch):
    fake = FakeMac()
    monkeypatch.setattr(codex_desktop.sys, "platform", "darwin")
    monkeypatch.setattr(codex_desktop, "_ps", lambda: list(fake.procs))
    monkeypatch.setattr(codex_desktop, "_env_codex_home",
                        lambda pid: (True, fake.env.get(pid)))
    monkeypatch.setattr(codex_desktop.subprocess, "run",
                        lambda argv, **kw: (fake.run_override or fake.run)(argv, **kw))
    monkeypatch.setattr(
        codex_desktop, "_bundle_info",
        lambda bundle: (codex_desktop.BUNDLE_ID, "ChatGPT") if bundle.endswith("ChatGPT.app")
        else ("com.example.helper", ""),
    )
    monkeypatch.setattr(codex_desktop, "_POLL_S", 0)
    monkeypatch.setattr(codex_desktop, "QUIT_TIMEOUT_S", 0.05)
    monkeypatch.setattr(codex_desktop, "LAUNCH_TIMEOUT_S", 0.05)
    return fake


def _history(home, *turns, written_ago: float = 3600) -> None:
    db = home / "thread_history_1.sqlite"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE thread_turns (thread_id TEXT, turn_id TEXT, status TEXT,"
                " started_at INTEGER, completed_at INTEGER)")
    con.executemany("INSERT INTO thread_turns VALUES ('t', ?, ?, ?, NULL)",
                    [(str(i), status, int(started)) for i, (status, started) in enumerate(turns)])
    con.commit()
    con.close()
    stamp = time.time() - written_ago
    os.utime(db, (stamp, stamp))


@pytest.fixture
def accounts(tmp_path, monkeypatch, mac):
    backup = tmp_path / "backup"
    home = tmp_path / "codex-home"
    home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(home))
    monkeypatch.setattr(codex, "get_backup_root", lambda: backup)
    monkeypatch.setattr(codex_desktop, "_same_home",
                        lambda value, _home: value in (None, str(home)))
    switcher = codex.CodexAccountSwitcher()
    for email, account in (("one@example.com", "acct-1"), ("two@example.com", "acct-2")):
        (home / "auth.json").write_text(json.dumps(_auth(email, account)), encoding="utf-8")
        switcher.add_account(assume_yes=True)
    switcher.restart_desktop = True
    _history(home, ("completed", NOW - 600))
    return switcher, home


def _live(home) -> dict:
    return json.loads((home / "auth.json").read_text(encoding="utf-8"))


def test_idle_desktop_is_quit_switched_and_reopened(accounts, mac):
    switcher, home = accounts
    old_main = mac.start_app()

    result = switcher.switch_to("1", json_output=True)

    assert result["switched"] and result["desktop"]["state"] == "restarted"
    assert result["desktop"]["bundle"] == BUNDLE
    assert mac.commands() == ["osascript", "open"]
    assert mac.calls[0][-1] == BUNDLE and mac.calls[1] == ["open", BUNDLE]
    assert old_main not in mac.apps() and len(mac.apps()) == 1
    assert _live(home) == _auth("one@example.com", "acct-1")
    assert codex_desktop.pending_handoff(switcher) is None


def test_desktop_not_running_switches_without_launching(accounts, mac):
    switcher, home = accounts

    result = switcher.switch_to("1", json_output=True)

    assert result["switched"] and result["desktop"]["state"] == "not-running"
    assert "osascript" not in mac.commands() and "open" not in mac.commands()
    assert mac.apps() == []
    assert _live(home) == _auth("one@example.com", "acct-1")


def test_already_selected_account_leaves_the_app_alone(accounts, mac):
    switcher, _home = accounts
    mac.start_app()

    result = switcher.switch_to("2", json_output=True)

    assert not result["switched"] and result["reason"] == "already-active"
    assert mac.calls == []


@pytest.mark.parametrize("turns, written_ago, detail", [
    ((("inProgress", NOW - 60),), 3600, "1 Codex turn(s) in progress"),
    ((("completed", NOW - 30),), 5, "active"),
])
def test_busy_desktop_waits_without_touching_anything(accounts, mac, turns, written_ago, detail):
    switcher, home = accounts
    (home / "thread_history_1.sqlite").unlink()
    _history(home, *turns, written_ago=written_ago)
    mac.start_app()
    before = _live(home)

    result = switcher.switch_to("1", json_output=True)

    assert not result["switched"] and result["reason"] == "desktop-busy"
    assert detail in result["message"]
    assert mac.calls == [] and _live(home) == before


def test_stale_in_progress_turn_from_a_previous_launch_is_ignored(accounts, mac):
    switcher, home = accounts
    (home / "thread_history_1.sqlite").unlink()
    _history(home, ("inProgress", NOW - 7200))  # Before the app started.
    mac.start_app(started=NOW - 3600)

    assert switcher.switch_to("1", json_output=True)["switched"]


def test_unreadable_history_is_not_treated_as_idle(accounts, mac):
    switcher, home = accounts
    (home / "thread_history_1.sqlite").unlink()
    mac.start_app()

    result = switcher.switch_to("1", json_output=True)

    assert result["reason"] == "desktop-busy" and "not found" in result["message"]
    assert mac.calls == []


@pytest.mark.parametrize("setup, needle", [
    (lambda mac: (mac.start_app(), mac.start_app("/Applications/Codex2/ChatGPT.app")),
     "2 Codex desktop instances"),
    (lambda mac: (mac.start_app(), mac.env.__setitem__(
        next(p.pid for p in mac.procs if p.exe == SERVER), "/elsewhere")),
     "does not use CODEX_HOME"),
])
def test_ambiguous_environments_are_refused(accounts, mac, setup, needle):
    switcher, home = accounts
    setup(mac)
    before = _live(home)

    with pytest.raises(SwitchError, match=needle):
        switcher.switch_to("1", json_output=True)

    assert "osascript" not in mac.commands() and _live(home) == before


def test_codex_cli_sharing_the_home_waits_instead_of_failing(accounts, mac):
    switcher, home = accounts
    mac.start_app()
    mac.spawn_cli(None)
    before = _live(home)

    result = switcher.switch_to("1", json_output=True)

    assert result["reason"] == "desktop-busy" and "shares this CODEX_HOME" in result["message"]
    assert "osascript" not in mac.commands() and _live(home) == before


def test_codex_cli_in_another_home_does_not_block(accounts, mac):
    switcher, _home = accounts
    mac.start_app()
    mac.spawn_cli("/tmp/other-codex-home")

    assert switcher.switch_to("1", json_output=True)["switched"]


@pytest.mark.parametrize("mode, needle", [
    ("refuse", "quit request failed"),
    ("denied", "Automation"),
    ("hang", "did not exit"),
    ("reopen", "reopened during the switch"),
])
def test_failed_quit_keeps_the_login_and_clears_the_marker(accounts, mac, mode, needle):
    switcher, home = accounts
    mac.start_app()
    mac.quit_mode = mode
    before = _live(home)

    with pytest.raises(SwitchError, match=needle):
        switcher.switch_to("1", json_output=True)

    assert _live(home) == before
    assert "open" not in mac.commands()
    assert codex_desktop.pending_handoff(switcher) is None


def test_late_switch_failure_reopens_the_app_on_the_old_login(accounts, mac, monkeypatch):
    switcher, home = accounts
    mac.start_app()
    before = _live(home)

    def boom(*_a, **_kw):
        raise SwitchError("Codex state changed during switch; retry")

    monkeypatch.setattr(switcher, "_switch_auth", boom)
    with pytest.raises(SwitchError, match="state changed"):
        switcher.switch_to("1", json_output=True)

    assert mac.commands() == ["osascript", "open"] and len(mac.apps()) == 1
    assert _live(home) == before
    assert codex_desktop.pending_handoff(switcher) is None


def test_relaunch_failure_is_reported_after_a_completed_switch(accounts, mac):
    switcher, home = accounts
    mac.start_app()
    mac.launch_ok = False

    result = switcher.switch_to("1", json_output=True)

    assert result["switched"] and result["desktop"]["state"] == "relaunch-failed"
    assert _live(home) == _auth("one@example.com", "acct-1")


def test_interrupted_handoff_reopens_the_app_once(accounts, mac):
    switcher, _home = accounts
    codex_desktop._write_marker(switcher, phase="launching", bundle=BUNDLE, to=1)

    assert "Reopened" in codex_desktop.recover_pending(switcher)
    assert mac.commands() == ["open"] and len(mac.apps()) == 1
    assert codex_desktop.recover_pending(switcher) is None
    assert mac.commands() == ["open"]


def test_interrupted_handoff_does_not_open_a_second_instance(accounts, mac):
    switcher, _home = accounts
    mac.start_app()
    codex_desktop._write_marker(switcher, phase="switching", bundle=BUNDLE, to=1)

    assert codex_desktop.recover_pending(switcher) is None
    assert mac.calls == [] and codex_desktop.pending_handoff(switcher) is None


def test_concurrent_handoff_is_refused(accounts, mac):
    switcher, home = accounts
    mac.start_app()
    holder = codex_desktop.FileLock(switcher.backup_dir / ".codex_desktop.lock")
    assert holder.acquire()
    try:
        result = switcher.switch_to("1", json_output=True)
    finally:
        holder.release()

    assert result["reason"] == "desktop-switch-in-progress" and mac.calls == []
    assert _live(home) == _auth("two@example.com", "acct-2")


def test_marker_holds_no_credentials(accounts, mac, monkeypatch):
    switcher, _home = accounts
    mac.start_app()
    seen = []
    real = codex_desktop._write_marker

    def spy(sw, **fields):
        real(sw, **fields)
        seen.append(codex_desktop._marker_path(sw).read_text(encoding="utf-8"))

    monkeypatch.setattr(codex_desktop, "_write_marker", spy)
    switcher.switch_to("1", json_output=True)

    assert [json.loads(text)["phase"] for text in seen] == ["closed", "switching", "launching"]
    for text in seen:
        assert "token" not in text and "@" not in text
    if sys.platform != "win32":
        assert codex_desktop._marker_path(switcher).parent.stat().st_mode & 0o077 == 0


def test_setting_enables_the_handoff_for_new_switchers(tmp_path, monkeypatch):
    backup = tmp_path / "backup"
    monkeypatch.setattr(codex, "get_backup_root", lambda: backup)
    assert codex.CodexAccountSwitcher().restart_desktop is False
    set_setting(backup, "codex.restartDesktop", "true")
    assert codex.CodexAccountSwitcher().restart_desktop is True


def _engine(switcher, events, **kw):
    settings = AutoSwitchSettings(threshold=80, cooldown_seconds=600)
    return CodexAutoSwitchEngine(switcher, settings, events.append, **kw)


def _over_limit(monkeypatch, switcher):
    from claude_swap.models import AccountSnapshot, AccountsSnapshot
    from claude_swap.usage_store import UsageEntry

    def snap(fetch=None):
        rows = tuple(
            AccountSnapshot(number=n, email=f"{n}@example.com", org_name="Codex", org_uuid=n,
                            is_active=n == "2", kind="oauth", switchable=True,
                            usage=UsageEntry(last_good={"five_hour": {"pct": pct}}))
            for n, pct in (("1", 10), ("2", 95))
        )
        return AccountsSnapshot("2", rows, taken_at=0)

    monkeypatch.setattr(switcher, "accounts_snapshot", snap)


def test_auto_switch_waits_for_a_busy_desktop_without_cooldown(accounts, mac, monkeypatch):
    switcher, home = accounts
    (home / "thread_history_1.sqlite").unlink()
    _history(home, ("inProgress", NOW - 10))
    mac.start_app()
    _over_limit(monkeypatch, switcher)
    events = []

    assert _engine(switcher, events).tick() is TickOutcome.NO_ACTION

    assert events[-1].kind == "no-switch" and events[-1].reason == "desktop-busy"
    assert not (switcher.backup_dir / STATE_FILENAME).exists()
    assert mac.calls == []


def test_auto_switch_restarts_an_idle_desktop(accounts, mac, monkeypatch):
    switcher, home = accounts
    mac.start_app()
    _over_limit(monkeypatch, switcher)
    events = []

    assert _engine(switcher, events).tick() is TickOutcome.SWITCHED

    assert events[-1].kind == "switch"
    assert "restarted on Account 1" in events[-1].warnings[0]
    assert mac.commands() == ["osascript", "open"]
    assert _live(home) == _auth("one@example.com", "acct-1")


def test_auto_dry_run_only_describes_the_desktop_plan(accounts, mac, monkeypatch):
    switcher, home = accounts
    mac.start_app()
    _over_limit(monkeypatch, switcher)
    events = []

    assert _engine(switcher, events, dry_run=True).tick() is TickOutcome.SWITCHED

    assert events[-1].dry_run and "would quit and reopen" in events[-1].warnings[0]
    assert mac.calls == [] and _live(home) == _auth("two@example.com", "acct-2")


def test_desktop_bundle_matches_only_the_main_binary(monkeypatch):
    monkeypatch.setattr(
        codex_desktop, "_bundle_info",
        lambda bundle: (codex_desktop.BUNDLE_ID, "ChatGPT")
        if bundle == BUNDLE else ("com.openai.codex.helper", "x"),
    )
    assert codex_desktop._desktop_bundle(MAIN) == BUNDLE
    assert codex_desktop._desktop_bundle(SERVER) is None
    assert codex_desktop._desktop_bundle(f"{BUNDLE}/Contents/MacOS/Other") is None
    assert codex_desktop._desktop_bundle("ChatGPT.app/Contents/MacOS/ChatGPT") is None


def test_preflight_refusal_never_quits_the_app(accounts, mac, monkeypatch):
    from claude_swap import codex_session

    switcher, home = accounts
    mac.start_app()
    real = codex_session.slot_custody
    monkeypatch.setattr(codex_session, "slot_custody", lambda sw, n, **kw: (
        codex_session.Custody("live", "running") if n == "1" else real(sw, n, **kw)))

    with pytest.raises(SwitchError, match="session conflict"):
        switcher.switch_to("1", json_output=True)

    assert mac.calls == [] and _live(home) == _auth("two@example.com", "acct-2")


def test_lingering_helper_does_not_hold_up_the_handoff(accounts, mac):
    switcher, home = accounts
    mac.start_app()
    mac.quit_leaving(lambda p: p.exe == "/bin/zsh")  # A background job outlives the app.

    assert switcher.switch_to("1", json_output=True)["desktop"]["state"] == "restarted"
    assert _live(home) == _auth("one@example.com", "acct-1")


@pytest.mark.parametrize("keep, needle", [
    (lambda p: p.exe == SERVER, "did not exit"),  # The app-server could still write.
    (None, "shares this CODEX_HOME"),  # A new codex appeared after the quit.
])
def test_failure_after_the_quit_reopens_the_app(accounts, mac, keep, needle):
    switcher, home = accounts
    mac.start_app()
    if keep is None:
        mac.quit_leaving(lambda p: False)
        original = mac.run_override

        def run(argv, **kw):
            result = original(argv, **kw)
            if argv[0] == "osascript":
                mac.spawn_cli(None)
            return result
        mac.run_override = run
    else:
        mac.quit_leaving(keep)

    with pytest.raises(SwitchError, match=needle):
        switcher.switch_to("1", json_output=True)

    assert _live(home) == _auth("two@example.com", "acct-2")
    assert "open" in mac.commands() and mac.apps()  # Not left closed.
    assert codex_desktop.pending_handoff(switcher) is None


def test_old_instance_reopened_mid_switch_is_not_reported_as_restarted(
        accounts, mac, monkeypatch):
    switcher, _home = accounts
    mac.start_app()
    real = switcher._switch_auth

    def reopen_then_switch(*a, **kw):
        mac.start_app(started=time.time() - 1)  # Loaded the old auth.json.
        return real(*a, **kw)

    monkeypatch.setattr(switcher, "_switch_auth", reopen_then_switch)
    result = switcher.switch_to("1", json_output=True)

    assert result["switched"] and result["desktop"]["state"] != "restarted"
    assert "old login" in result["desktop"]["message"]


def test_rotation_skips_conflicting_candidates_without_restarts(accounts, mac, monkeypatch):
    from claude_swap import codex_session

    switcher, home = accounts
    (home / "auth.json").write_text(json.dumps(_auth("three@example.com", "acct-3")))
    switcher.add_account(assume_yes=True)  # Active is now 3; rotation tries 1, then 2.
    mac.start_app()
    preflight = switcher._switch_preflight

    def refuse_one(identifier):
        if identifier == "1":
            raise SwitchError("Codex session conflict: Account 1 session is live")
        return preflight(identifier)

    monkeypatch.setattr(switcher, "_switch_preflight", refuse_one)
    monkeypatch.setattr(codex_session, "slot_custody",
                        lambda *a, **kw: codex_session.Custody("none", "no_session_home"))

    result = switcher.switch(strategy=None, json_output=True)

    assert result["to"]["number"] == 2
    assert mac.commands().count("osascript") == 1


def test_auto_backs_off_after_a_failed_handoff(accounts, mac, monkeypatch):
    switcher, home = accounts
    mac.start_app()
    _over_limit(monkeypatch, switcher)

    def boom(*_a, **_kw):
        raise SwitchError("Codex state changed during switch; retry")

    monkeypatch.setattr(switcher, "_switch_auth", boom)
    events = []
    engine = _engine(switcher, events)
    assert engine.tick() is TickOutcome.ERROR
    stamp = time.time() - 3600
    os.utime(home / "thread_history_1.sqlite", (stamp, stamp))
    assert engine.tick() is TickOutcome.NO_ACTION

    assert events[-1].reason == "desktop-backoff"
    assert mac.commands().count("osascript") == 1


def test_in_progress_refusal_is_printed(accounts, mac, capsys):
    switcher, _home = accounts
    mac.start_app()
    holder = codex_desktop.FileLock(switcher.backup_dir / ".codex_desktop.lock")
    assert holder.acquire()
    try:
        switcher.switch_to("1")
    finally:
        holder.release()
    assert "in progress" in capsys.readouterr().out


def test_cli_codex_without_a_desktop_does_not_block(accounts, mac):
    switcher, home = accounts
    mac.spawn_cli(None)

    result = switcher.switch_to("1", json_output=True)

    assert result["switched"] and result["desktop"]["state"] == "not-running"
    assert mac.calls == [["pgrep", "-x", "codex"]]


@pytest.mark.parametrize("fields", [
    {"phase": "quitting"},  # The app was never confirmed closed.
    {"phase": "closed", "startedAt": NOW - 2 * 3600},  # Stale.
])
def test_recovery_ignores_unconfirmed_or_stale_markers(accounts, mac, fields):
    switcher, _home = accounts
    codex_desktop._write_marker(switcher, bundle=BUNDLE, to=1, **fields)

    assert codex_desktop.recover_pending(switcher) is None
    assert mac.calls == [] and codex_desktop.pending_handoff(switcher) is None


def test_same_home_defaults_to_dot_codex(tmp_path, monkeypatch):
    monkeypatch.setattr(codex_desktop.Path, "home", lambda: tmp_path)
    (tmp_path / ".codex").mkdir()
    assert codex_desktop._same_home(None, tmp_path / ".codex")
    assert codex_desktop._same_home(str(tmp_path / ".codex"), tmp_path / ".codex")
    assert not codex_desktop._same_home(str(tmp_path / "other"), tmp_path / ".codex")


def test_restart_setting_is_reread_by_long_lived_switchers(tmp_path, monkeypatch):
    backup = tmp_path / "backup"
    monkeypatch.setattr(codex, "get_backup_root", lambda: backup)
    switcher = codex.CodexAccountSwitcher()
    set_setting(backup, "codex.restartDesktop", "true")
    assert switcher.restart_desktop is True


LIMIT = json.dumps({"message": "You've hit your usage limit.",
                    "codexErrorInfo": "usageLimitExceeded"})


def _threads(home, *rows, written_ago: float = 3600) -> None:
    """Newer thread history + state DBs: (thread, ordinal, status, error, done_ago, extra)."""
    history = sqlite3.connect(home / "thread_history_2.sqlite")
    history.execute("CREATE TABLE thread_turns (thread_id TEXT, turn_id TEXT, rollout_ordinal"
                    " INTEGER, status TEXT, error_json TEXT, started_at INTEGER,"
                    " completed_at INTEGER)")
    state = sqlite3.connect(home / "state_5.sqlite")
    state.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, cwd TEXT, archived INTEGER,"
                  " thread_source TEXT, source TEXT, model TEXT, reasoning_effort TEXT)")
    for thread, ordinal, status, error, done_ago, extra in rows:
        done = int(time.time() - done_ago)
        history.execute("INSERT INTO thread_turns VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (thread, f"{thread}-{ordinal}", ordinal, status, error, done - 5, done))
        state.execute("INSERT OR IGNORE INTO threads VALUES (?, ?, ?, ?, ?, ?, ?)",
                      (thread, str(home), extra.get("archived", 0), extra.get("thread_source"),
                       extra.get("source", "vscode"), "gpt-test", "high"))
    for con in (history, state):
        con.commit()
        con.close()
    stamp = time.time() - written_ago
    os.utime(home / "thread_history_2.sqlite", (stamp, stamp))


@pytest.fixture
def popen(monkeypatch):
    calls = []
    monkeypatch.setattr(codex_desktop, "_bundled_cli", lambda bundle: "/fake/codex")
    monkeypatch.setattr(codex_desktop.subprocess, "Popen",
                        lambda argv, **kw: calls.append((argv, kw)))
    return calls


def test_restart_resumes_limit_stopped_threads_once(accounts, mac, popen):
    switcher, home = accounts
    set_setting(switcher.backup_dir, "codex.resumeAfterRestart", "true")
    _threads(home,
             ("stopped", 0, "completed", None, 900, {}),
             ("stopped", 1, "failed", LIMIT, 600, {}),
             ("later-ok", 0, "failed", LIMIT, 600, {}),
             ("later-ok", 1, "completed", None, 300, {}),  # Already continued by hand.
             ("old", 0, "failed", LIMIT, 7 * 3600, {}),
             ("other-error", 0, "failed", json.dumps({"codexErrorInfo": "other"}), 600, {}),
             ("archived", 0, "failed", LIMIT, 600, {"archived": 1}),
             ("created", 0, "failed", LIMIT, 600, {"thread_source": "agent_created_thread"}),
             ("subagent", 0, "failed", LIMIT, 600,
              {"source": '{"subagent":{"thread_spawn":{"depth":1}}}'}))
    mac.start_app()

    result = switcher.switch_to("1", json_output=True)

    assert result["desktop"]["state"] == "restarted"
    assert result["desktop"]["resumed"] == ["stopped"]
    assert "Continuing 1 thread" in result["desktop"]["message"]
    (argv, kw), = popen
    assert argv == ["/fake/codex", "exec", "--skip-git-repo-check", "resume",
                    "-m", "gpt-test", "-c", "model_reasoning_effort=high", "stopped", "continue"]
    assert kw["cwd"] == str(home) and kw["env"]["CODEX_HOME"] == str(home)
    assert kw["start_new_session"] and kw["stdout"] is subprocess.DEVNULL

    switcher.switch_to("2", json_output=True)  # Same stopped turn: never sent twice.
    assert len(popen) == 1


def test_resume_is_off_by_default(accounts, mac, popen):
    switcher, home = accounts
    _threads(home, ("stopped", 0, "failed", LIMIT, 600, {}))
    mac.start_app()

    result = switcher.switch_to("1", json_output=True)

    assert result["desktop"]["state"] == "restarted" and "resumed" not in result["desktop"]
    assert popen == []


def test_no_resume_without_a_confirmed_restart(accounts, mac, popen):
    switcher, home = accounts
    set_setting(switcher.backup_dir, "codex.resumeAfterRestart", "true")
    _threads(home, ("stopped", 0, "failed", LIMIT, 600, {}))
    mac.start_app()
    mac.launch_ok = False

    result = switcher.switch_to("1", json_output=True)

    assert result["desktop"]["state"] == "relaunch-failed" and popen == []
