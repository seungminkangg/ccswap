# Run multiple accounts at the same time (session mode)

Launch Claude Code as a specific account in the current terminal only — every other terminal and the VS Code extension stay on your default account, so two accounts can work in parallel.

```bash
ccswap run 2                     # launch Claude Code as account 2, here only
ccswap run user@example.com      # by email
ccswap run 2 -- --resume         # everything after '--' is forwarded to claude
ccswap run 2 --share-history     # share your chat history with this account too
ccswap run 2 --require-session   # refuse rather than run plain claude if 2 is the default login
```

Sessions use your normal `~/.claude` setup (settings, CLAUDE.md, skills, MCP servers, etc.), but each account keeps its own chat history — pass `--share-history` if you want your accounts to continue the same conversations.

Running the account that is already your default login launches plain `claude` on that login instead of a session (a second copy of the active credential would go stale). Scripts that need the isolation guaranteed can pass `--require-session`, which refuses in that case instead.
  
A session refreshes its own copy of the account's token, so once it exits, the credential it rotated is captured back into the account's stored backup before a switch or usage check uses that backup. While a session is still running, `ccswap switch` refuses to move the default login onto its account if the stored backup has already fallen behind (activating it could only fail); exit the session first, or pick another account. While a session runs, its account's usage is read with the session's own credential and never refreshed by ccswap; a read the server refuses shows as token expired, and is not requested again, until the session renews the credential on its next call.

## Profile safety when session records cannot be read

If session records or profile folders can't be read, ccswap can't tell whether
Claude is running. It refuses profile removal and defers token refresh;
backup updates preserve the profile's credentials until it is safe to replace
them. Restore access and retry.

## Sharing details — MCP servers & chat history

- With `--share-history`, a session started under one account shows up in `--resume` under the others, and nothing already saved is lost.
- User-scope MCP servers (`claude mcp add -s user`) are mirrored from your default profile on every launch — manage them there; changes made inside a session don't persist. Definitions are copied as-is (including inline `env`/`headers` values), but MCP OAuth logins are not — HTTP servers may ask you to authenticate once per profile via `/mcp`.
- `--no-share` turns sharing off and removes the mirrored MCP config (profiles that never mirrored are left alone).

## Map accounts to directories — auto-pick per repo

Bind a directory to an account, and a bare `ccswap run` there launches that account in session mode — e.g. work account in work repos, personal elsewhere:

```bash
ccswap map 2 ~/work/client-app   # map a directory to account 2
ccswap map user@example.com      # map the current directory
ccswap map                       # list mappings
ccswap unmap ~/work/client-app   # remove one (defaults to current directory)

cd ~/work/client-app/src
ccswap run                       # → account 2, session mode
```

Subfolders inherit the nearest mapped ancestor. In an unmapped directory, `ccswap run` just launches plain `claude` with your default login. Mappings are per-machine (not part of `ccswap export`) and are cleaned up when their account is removed.
