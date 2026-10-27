from claude_swap.session import AUTH_OVERRIDE_ENV_VARS
from claude_swap.setup_token_usage import parse_usage_output


def test_parse_screen_reader_usage_output():
    output = b"""\
Settings  Status   Config   Usage   Stats\r\n
Current session\r\n
12% 12% used\r\n
Resets 5:40pm (Asia/Seoul)\r\n
Current week (all models)\r\n
5% 5% used\r\n
Resets Aug 17 at 6am (Asia/Seoul)\r\n
"""
    assert parse_usage_output(output) == {
        "five_hour": {
            "pct": 12.0,
            "clock": "5:40pm (Asia/Seoul)",
            "countdown": "?",
        },
        "seven_day": {
            "pct": 5.0,
            "clock": "Aug 17 at 6am (Asia/Seoul)",
            "countdown": "?",
        },
    }


def test_parse_requires_both_windows():
    assert parse_usage_output("Current session\n4% used\nResets 5:10pm") is None


def test_parse_strips_ansi_sequences():
    output = (
        b"\x1b[2KCurrent session\r\n4% used\r\nResets 5:10pm (Asia/Seoul)\r\n"
        b"Current week (all models)\r\n1% used\r\n"
        b"Resets Aug 13 at 11pm (Asia/Seoul)\x1b[7A"
    )
    usage = parse_usage_output(output)
    assert usage is not None
    assert usage["five_hour"]["pct"] == 4.0
    assert usage["seven_day"]["pct"] == 1.0


def test_canonical_auth_overrides_include_fd_credentials():
    assert "CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR" in AUTH_OVERRIDE_ENV_VARS
    assert "CLAUDE_CODE_API_KEY_FILE_DESCRIPTOR" in AUTH_OVERRIDE_ENV_VARS
