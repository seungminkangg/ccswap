"""Usage collection for inference-only Claude Code setup tokens.

Anthropic's OAuth usage endpoint requires scopes that ``claude setup-token``
credentials do not carry.  Claude Code can still show the account's limits in
its built-in ``/usage`` screen, so setup-token accounts are queried through a
short-lived, isolated Claude Code session instead of repeatedly sending a
request that is guaranteed to fail with 403 (and eventually 429).

The probe deliberately uses safe mode and screen-reader output: custom hooks,
plugins, MCP servers, and project instructions stay disabled, while the
built-in usage view is emitted as stable plain text.  Credentials remain in
the account's existing ``CLAUDE_CONFIG_DIR`` and are never placed in argv,
stdout, or the environment.
"""

from __future__ import annotations

import os
import re
import select
import shutil
import signal
import subprocess
import time
from pathlib import Path

from claude_swap.session import AUTH_OVERRIDE_ENV_VARS


DEFAULT_TIMEOUT_S = 30.0
MAX_OUTPUT_BYTES = 256 * 1024

_ANSI_RE = re.compile(
    rb"\x1b(?:\][^\x07]*(?:\x07|\x1b\\)|\[[0-?]*[ -/]*[@-~]|[()][0-2A-Z@])"
)
_WINDOW_RE = re.compile(
    r"(?P<label>Current session|Current week \(all models\))\s+"
    r"(?:(?:\d+(?:\.\d+)?)%\s+)?(?P<pct>\d+(?:\.\d+)?)% used\s+"
    r"Resets\s+(?P<reset>[^\r\n]+)",
    re.IGNORECASE,
)

class SetupTokenUsageError(RuntimeError):
    """The Claude Code usage probe could not produce both quota windows."""


def parse_usage_output(raw: bytes | str) -> dict | None:
    """Parse screen-reader ``/usage`` output into cswap's usage shape."""
    if isinstance(raw, bytes):
        raw = _ANSI_RE.sub(b"", raw).decode(errors="replace")
    else:
        raw = _ANSI_RE.sub(b"", raw.encode()).decode(errors="replace")

    result: dict[str, dict] = {}
    for match in _WINDOW_RE.finditer(raw):
        label = match.group("label").lower()
        key = "five_hour" if "session" in label else "seven_day"
        reset = match.group("reset").strip()
        result[key] = {
            "pct": float(match.group("pct")),
            # Claude renders this already localized to the user's timezone.
            # It is display-safe but intentionally not promoted to resets_at:
            # the human-form string is not a stable machine timestamp.
            "clock": reset,
            "countdown": "?",
        }

    return result if "five_hour" in result and "seven_day" in result else None


def fetch_setup_token_usage(
    config_dir: Path,
    *,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    claude_bin: str | None = None,
) -> dict:
    """Return quota usage by driving Claude Code's built-in ``/usage`` view.

    POSIX pseudo-terminals are required because Claude Code intentionally omits
    subscription windows from non-interactive output.  The subprocess is
    bounded by ``timeout_s`` and always terminated as a process group.
    """
    if os.name != "posix":
        raise SetupTokenUsageError(
            "setup-token usage probing requires a POSIX pseudo-terminal"
        )

    executable = claude_bin or shutil.which("claude")
    if not executable:
        raise SetupTokenUsageError("Claude Code was not found on PATH")

    import pty

    master_fd, slave_fd = pty.openpty()
    env = {k: v for k, v in os.environ.items() if k not in AUTH_OVERRIDE_ENV_VARS}
    env["CLAUDE_CONFIG_DIR"] = str(config_dir)
    argv = [executable, "--safe-mode", "--ax-screen-reader"]

    process: subprocess.Popen[bytes] | None = None
    output = bytearray()
    sent_usage = False
    answered_trust = False
    deadline = time.monotonic() + timeout_s
    try:
        process = subprocess.Popen(
            argv,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            cwd=config_dir,
            env=env,
            start_new_session=True,
            close_fds=True,
        )
        os.close(slave_fd)
        slave_fd = -1

        while time.monotonic() < deadline:
            ready, _, _ = select.select([master_fd], [], [], 0.25)
            if not ready:
                if process.poll() is not None:
                    break
                continue
            try:
                chunk = os.read(master_fd, 8192)
            except OSError:
                break
            if not chunk:
                break
            output.extend(chunk)
            if len(output) > MAX_OUTPUT_BYTES:
                del output[: len(output) - MAX_OUTPUT_BYTES]

            plain = _ANSI_RE.sub(b"", output)
            if b"Enter y/n:" in plain and not answered_trust:
                os.write(master_fd, b"y\r")
                answered_trust = True
                continue

            # Screen-reader mode renders the input prompt as a literal '$'.
            # Wait for it so /usage is not swallowed by the trust dialog or
            # startup rendering.
            if not sent_usage and re.search(rb"(?:^|[\r\n])\$", plain):
                os.write(master_fd, b"/usage\r")
                sent_usage = True

            usage = parse_usage_output(bytes(output)) if sent_usage else None
            if usage is not None:
                return usage

        reason = "timed out" if process.poll() is None else "exited early"
        raise SetupTokenUsageError(f"Claude Code usage probe {reason}")
    finally:
        if slave_fd >= 0:
            os.close(slave_fd)
        try:
            os.close(master_fd)
        except OSError:
            pass
        if process is not None and process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=1.0)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=1.0)
