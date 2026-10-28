# Codex CLI accounts

`ccswap` can also save and switch Codex CLI logins. Log into each account with `codex login`, then save it before logging into the next one:

```bash
codex login
ccswap codex add

# Log in to the next Codex account, then save it too.
codex login
ccswap codex add

ccswap codex list                 # accounts tagged by plan, e.g. [Codex Team]
ccswap codex usage                # Quota, credit allowance, and banked resets
ccswap codex switch 1
ccswap codex switch                 # rotate to the next saved account
ccswap codex switch --strategy best
ccswap codex switch --strategy next-available
ccswap codex switch --strategy consume-first
ccswap codex auto --once            # switch when active quota reaches the threshold
ccswap codex remove 2
```

Usage-aware strategies consider switchable, enabled ChatGPT accounts only; API-key accounts have no subscription quota and are skipped.

Codex switching preserves the rest of `CODEX_HOME` (configuration, skills, sessions, and history) and replaces only `auth.json`. Restart Codex after switching so its running process loads the selected login, or let ccswap restart the desktop app ([Codex desktop restart](#codex-desktop-restart-macos-codexrestartdesktop)). For ChatGPT-backed file logins, the dashboard reads the same read-only Codex rate-limit endpoints used by Codex and shows the weekly window with a live reset countdown, the workspace/purchased credit balance, the per-user credit allowance when the service reports one, plus the number of banked resets and the earliest available reset's expiry. Credits, per-user allowances, and banked resets are shown separately because they are different resources. Window labels are derived from the duration reported by Codex, so older accounts that still return a 5-hour window remain correctly labelled. Each account is labelled by its ChatGPT plan (`Codex Team`, `Codex Pro`, `Codex Plus`, …) — read from the login token, the closest local equivalent to Claude Code's org name, since Codex stores no workspace name on disk. API-key accounts have no ChatGPT subscription quota, so they remain status-only. `ccswap codex auto` honors `autoswitch.strategy` (`best` or `consume-first`) and the per-window thresholds `autoswitch.threshold5h` and `autoswitch.threshold7d` (Codex Weekly), and uses the same threshold, cooldown, `--once`, `--dry-run`, and JSON event controls as Claude auto-switching; it prepares the account for the next Codex launch and reports when a restart is needed.

Codex must use its documented file credential store. If your `~/.codex/config.toml` says `cli_auth_credentials_store = "keyring"`, change it to `"file"`, run `codex login`, then add the account. This deliberate restriction avoids writing a guessed OS-keyring entry.

## Codex desktop restart (macOS, `codex.restartDesktop`)

The Codex desktop app (bundle ID `com.openai.codex`, installed as `Codex.app` or `ChatGPT.app`) keeps the login in its bundled `codex app-server`, so a plain switch only takes effect after a restart. Turn on the desktop handoff to have ccswap do that restart for you:

```bash
ccswap config set codex.restartDesktop true   # every Codex switch path: manual, rotation, auto, menu bar
ccswap codex switch 2 --restart-desktop       # one switch only (--no-restart-desktop skips it)
ccswap codex desktop                          # current state: running bundle, idle/busy, blockers
ccswap codex desktop --json
```

The menu bar's Codex submenu has the same **Restart Codex desktop on switch** toggle and shows the desktop state while it is on. The default is off, which keeps the switch-and-restart-yourself behaviour above.

With the setting on, a switch:

1. Finds exactly one desktop instance of your user. Two instances or a desktop that uses a different `CODEX_HOME` refuse the switch with the account unchanged. Another `codex` process (CLI, IDE plugin) sharing the same `CODEX_HOME` could write the old login back, so the switch waits for it to exit (`desktop-busy`, as in step 2). A `codex` running in another `CODEX_HOME` (for example `ccswap codex run`) does not block.
2. Waits while the desktop is working: any turn started since the app launched is still `inProgress` in Codex's thread history, or the history was written in the last 60 seconds. Missing or unreadable history also waits; ccswap never assumes idle. A waiting switch changes nothing, reports `desktop-busy` (exit code 1 for `codex switch`), and auto-switch retries on the next poll without starting its cooldown.
3. Asks the app to quit normally (the same Apple Event as ⌘Q) and waits up to 30 seconds for the app and its `codex` app-servers to exit (other helpers may outlive it; they cannot write `auth.json`). A refused or slow quit (for example a confirmation dialog) stops the switch with the account unchanged. ccswap never sends signals or kills Codex.
4. Switches `auth.json` through the regular locked, lineage-checked switch. If that fails, the app is reopened on the unchanged login.
5. Reopens the same bundle that was running, waits for its app-server, and confirms the selected login is active. A desktop that was not running is not opened; selecting the already-active account does nothing.

Only an idle app is restarted, so no running turn is cut off. `--dry-run` auto-switching only describes what it would do to the desktop.

### Continuing threads the usage limit stopped (`codex.resumeAfterRestart`)

A thread whose turn failed with Codex's usage-limit error stays stopped after a switch; by default you reopen it and type `continue` yourself. To have ccswap do that once per stopped turn:

```bash
ccswap config set codex.resumeAfterRestart true   # needs codex.restartDesktop
```

The menu bar has the same **Continue limit-stopped threads after restart** toggle. After a confirmed restart onto the new login, ccswap picks threads whose latest turn failed with `usageLimitExceeded` in the last 6 hours (at most 5; archived and subagent threads are skipped) and continues each one inside Codex desktop: the bundled `codex queue` puts `continue` in the app's own message queue for that thread, then `open -g codex://threads/<thread>` opens the thread in the background so the app sends it with the thread's own model and settings. The turn runs in the desktop like one you typed, so you can watch, stop or steer it there. Each stopped turn is recorded in `<backup root>/codex/resumed_turns.json` before it is sent, so it is never continued twice, even if ccswap crashes. The switch result and menu-bar notice say how many threads were continued. While a continuation runs, the next desktop switch waits for it.

ccswap records an in-flight handoff in `<backup root>/codex/desktop_handoff.json` (bundle path and slot numbers, no credentials, 0600). If ccswap is interrupted after closing the app, the next switch, auto-switch poll or menu-bar tick reopens the app once and clears the record; it never retries in a loop. Only one handoff runs at a time (`.codex_desktop.lock`).

The handoff only coordinates with processes it can see. Another account switcher that rewrites `~/.codex/auth.json` on its own (for example Codex Switcher) must not run at the same time.

## Session mode (`codex run`)

Run a saved ChatGPT account in this terminal with a persistent, private `CODEX_HOME` per account. Your shell environment and default `auth.json` stay unchanged. Different accounts can run at the same time. Selecting the actual default login runs plain Codex with an isolation notice; `--require-session` refuses that shortcut. With no identifier, the current default must be a managed account. Disabled accounts cannot launch.

```bash
ccswap codex run 2
ccswap codex run user@example.com --require-session
ccswap codex run                            # actual managed default login
ccswap codex run 2 --shared                  # explicitly join an existing session
ccswap codex run 2 -- exec --json "Review this change"
ccswap codex run 2 -- resume --last
```

Wrapper flags (`--require-session`, `--shared`) must precede `--`. Everything after the first `--` goes to Codex unchanged, including option values that start with `-`. There is no wrapper `--json`, `--strategy`, mapping or recovery flag. Interactive, `resume`, `fork`, `exec` and `exec resume` are supported; remote, login/logout, app-server, cloud and daemon/service commands are refused. Interactive/resume/fork launches disable daemon startup. Without `--cd`, `resume` and `fork` run in the current directory, the one whose configuration was checked, rather than offering the session's recorded directory. A nonempty inherited `CODEX_HOME` is refused before account discovery.

One live session per account is the default. `--shared` joins the existing home without re-seeding its login: **refresh tokens may race, just as with several Codex processes in `~/.codex`**. Capture remains pending until all writers exit. Ctrl-C goes to Codex directly; on POSIX the supervisor forwards SIGTERM/SIGHUP.

### Requirements and policy refusals

Isolated session mode requires **stable Codex 0.164.0 or newer** and checks its version and `--no-daemon` capability without account credentials. This is a minimum and capability check, not a claim that every future release has been tested. The plain default-login shortcut skips the session probe and minimum version check; isolated macOS runs report the platform refusal before resolving or probing the binary. The executable is found on `PATH`. On Windows, ccswap requires native `codex.exe`: it can locate the native binary associated with a standard npm installation, but never executes `.cmd`/`.bat`/PowerShell shims with your arguments. If resolution fails, put native `codex.exe` on `PATH`.

Isolated sessions accept known, supported ChatGPT plans. Workspace-managed business/enterprise/education plans and unknown or missing plans are not supported yet; API-key and other authentication modes are also refused. Local system, user and project configuration is checked conservatively before a writer starts. Unreadable configuration, incompatible login/workspace policy, unsupported profiles, custom account routing, managed requirements that replace the backend, provider or credential store, or managed storage paths outside the session home cause a refusal rather than a policy bypass. A hard-linked session `auth.json` is refused, and on POSIX a reused session home is made private again (`0700`/`0600`). Account-routing environment overrides are removed only from the isolated child, with variable names reported on stderr, never their values.

**macOS isolated runs are currently refused:** `macOS session supervision awaits native CI validation.` The process backend is implemented and tested with mocks, but has no native CI validation yet. The default-login shortcut remains plain Codex.

### Shared definitions and private state

Present `config.toml`, `AGENTS.md`, `AGENTS.override.md`, `rules/` and `prompts/` are shared from the default Codex home. Each user-managed entry in `skills/` is shared separately; the directory itself and `skills/.system/` remain private. POSIX uses symlinks, so definition edits write through to the default home. On Windows, unavailable symlinks fall back to private copies **refreshed on every launch, including shared joins**; edits to those copies are overwritten.

Authentication and credential stores, MCP credentials/locks, rollouts (`sessions/`, `archived_sessions/`), history, thread locks, databases and sidecars, logs, memories, caches, daemon state and plugins remain private. Plugins and history sharing are deferred. MCP logins are per session home: expect a one-time re-authentication there; existing MCP tokens are never copied.

### Capture pending and JSON

Once the whole session home is proven idle, ccswap reconciles persisted credential rotations with the saved login. An initial child exiting is insufficient if descendants or another shared run can still write. **Capture pending** means that adoption is deferred; known live remaining writers preserve the child's exit code. Unknown liveness blocks credential writes and returns wrapper failure, retaining evidence. Conflicts preserve the files for explicit recovery.

`--worktree` and relative `sqlite_home`/`log_dir` overrides for resume/fork are unsupported syntax.

All run-wrapper notices go to stderr, leaving Codex stdout (including `exec --json`) untouched. Exit codes are `2` for parser/unsupported syntax errors, `1` for setup, policy, liveness or capture failures, otherwise Codex's exit code. POSIX signal death maps to `128 + signal`; Windows preserves the native nonnegative status.

`ccswap codex list --json` adds `session` to each account row. `ccswap codex status --json` adds `sessions: [{"number": N, "session": ...}]` for every saved account, including those without a home. A session has `state` (`absent`, `idle`, `live`, `uncertain`, `stale`, `conflict`), `capturePending`, `runCount` and `shared`. The actual default remains active; an isolated session does not become the default. See [JSON output](json.md#codex-session-fields).

When `codex/sessions/` is absent, status, plan labels and usage snapshots do not wait for the account-store lock. Access errors or malformed paths do not count as absence. Credential writers and refreshes still recheck custody under the lock. If the default `auth.json` is temporarily unreadable, backup credentials remain available for read-only quota requests; no refresh is authorized until the default login can be checked again.

### Resolving a session conflict

If both the session home and saved login moved, or their identities drifted, ccswap blocks switch/add/remove/run for that account by design. First make sure no Codex process uses that session home. Then move `<backup root>/codex/sessions/<N>-<email-slug>/` and, if present, its sibling `.<N>-<email-slug>.stale.json` (an interrupted credential update) **out of the `sessions/` directory**, keeping them if you want their history and evidence. Run `codex login` for a fresh login, then `ccswap codex add`, or remove the saved slot with `ccswap codex remove N`. Do not move a live or uncertain home to bypass its guard. Finished run records under the home's `runs/` are kept; a damaged record keeps the account uncertain until it is removed.

The backup root is `~/.local/share/claude-swap/` on Linux/WSL, or `<XDG_DATA_HOME>/claude-swap/` when `XDG_DATA_HOME` resolves to an absolute path (a leading `~` is expanded; empty/relative values use the default). Windows and macOS use `~/.claude-swap-backup/`. The folder suffix is the normalized email slug, so use the existing folder name rather than guessing it.

### Remaining risks

A foreign writer launched manually with `CODEX_HOME=<session home>` is outside the safety model. Process scans guard against observed writers but cannot promise safe token rotation against arbitrary external tools. If such a writer rotates that home while ccswap refreshes the inactive account, the refreshed login is kept in the saved backup and the account reports a conflict for explicit resolution. Shared config can change after preflight, and a remote token rotation that Codex never durably saved before a crash cannot be recovered. Likewise, if neither the session home nor the saved backup can be written after ccswap refreshed an inactive login (for example, a full disk), the new token is lost. The custody checks protect against accidents and crashes, not a hostile process running as the same user.


Use `ccswap codex redeem-reset NUM|EMAIL` to explicitly spend one banked reset for any saved ChatGPT account; this never happens automatically. The command selects the earliest-expiring available card and shows its expiry in the confirmation (`--yes` skips it and is required with `--json`). It sends exactly one redemption request for that selected card, never retries the POST, and re-reads usage afterward. If card details cannot be read, it refuses to spend an unspecified card. `reset` means accepted, with confirmation reported separately; `nothing_to_reset` keeps the card, `no_credit` means none is available, and `already_redeemed` means the card was already used. Check uncertain outcomes with `ccswap codex usage` before retrying.

When session mode is present, an explicit reset uses the selected default/backup access token and never rotates credentials in the POST. A still-valid backup token can redeem while that account has a live session. A 401 can refresh only through the guarded inactive-account path; live, uncertain or conflicting sessions refuse that refresh. The final POST rechecks account identity, session custody and credential consistency under the account-store lock. Uncertain/conflicted state or a pending credential transaction cancels redemption. An unreadable default login prevents refresh reconciliation before it can write.
