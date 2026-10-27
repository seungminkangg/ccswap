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
    monkeypatch.setattr(codex_desktop.subprocess, "run", fake.run)
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
    assert mac.calls == [] and mac.apps() == []
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
    (lambda mac: (mac.start_app(), mac.spawn_cli(None)), "shares this CODEX_HOME"),
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


def test_switch_failure_after_quit_reopens_the_app_on_the_old_login(accounts, mac, monkeypatch):
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

    assert [json.loads(text)["phase"] for text in seen] == ["quitting", "switching", "launching"]
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
