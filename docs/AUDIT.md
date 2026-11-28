# Audit of rbit 1.6.0 → 1.7.0

Scope: the HTTP server (`rbit serve` / `rbit mobile`), the desktop app, the terminal UI, the
sandbox, the iOS app, CI and release. What was fixed is in `CHANGELOG.md` (1.7.0); this file keeps
what was found, how it was checked, and what is still open.

## Method

`pytest` (all tests), `mypy` (whole package), `ruff` (`F,E9,B,PLE`), `bandit`, `pip-audit`;
manual reading of the server, desktop renderer and sandbox policy; the desktop UI was driven in a
browser (Playwright) and the TUI with Textual's `run_test` at 120×40, 80×24, 60×18 and 40×12.

## Fixed

| Area | Finding | Fix |
|---|---|---|
| Desktop | Shell commands ran unattended (no approval UI, approvals off) | `RBIT_APPROVALS=1` + approval dialog, sandbox `auto` by default |
| Approvals | "Always allow" on a 2-word prefix approved `python -c ...` | interpreters/launchers/`tool -opt` are exact-match only |
| Server | `ask`/`review`/`debug` silently became `build` (write access) | all six modes honoured |
| Server | `git status` in a cloned repo could run `core.fsmonitor` | repository-configured programs disabled, timeout |
| Server | token accepted in any URL; cookie accepted cross-origin | URL token only for the page, cookie needs a trusted Origin |
| Server | a schedule created over HTTP bypassed approvals | such schedules are not unattended; input validated |
| Server | `/v1/fs` broke on a symlinked workspace | resolved root |
| TUI | `ctrl+m`/`ctrl+h` arrive as Enter/Backspace and never fired | `f2`/`f3` (+ aliases); `f6`–`f9` for `ctrl+shift` combos |
| TUI | `bindings` in the config did nothing | rebinding applied to the live map |
| TUI | prompt box cut off on short terminals; fake "LSP ready"; red "Ollama offline" | adaptive layout, real LSP state, optional services hidden |
| CLI | `rbit plugin add/check/update/remove` unreachable | routed again |
| Sandbox | bubblewrap path did not protect `.git/config` or system secrets; macOS profile did not protect `.git/config` | aligned with the Landlock launcher |
| CI | mypy on 2 files, ruff on fatal rules only, no Python 3.13 | whole package, `F,E9,B,PLE`, 3.13 |
| Release | package-manager templates had placeholders | `scripts/render_distribution.py` + release step |

## Not verified / still open

- **Sandbox backends were not run live here.** The audit machine has neither Landlock nor
  bubblewrap, so `rbit sandbox check` could only be reviewed and unit-tested (generated argv and
  profile), not executed. Run it on Linux, macOS and Windows before relying on isolation.
- **macOS Seatbelt** allows Mach services (`security`, `osascript`) by design of `(allow default)`;
  keychain access through them is not blocked.
- **bubblewrap** binds the whole host root read-only; other users' home directories rely on file
  permissions.
- **iOS app** (reviewed, not built — no Xcode here, so no Swift edits were made):
  - Good: token in the Keychain (`ThisDeviceOnly`), ephemeral `URLSession`, ATS limited to local
    networking, the `rbit://connect` link only pre-fills the form (no auto-connect).
  - Open: only ~9 `accessibilityLabel`s in ~5900 lines and ~49 fixed-size fonts (VoiceOver and
    Dynamic Type); the token is sent in two headers (`Authorization` and `X-RBit-Token`); a bare
    address defaults to `http://` (blocked by ATS off the local network, which is the intended
    safety net).
- **Windows** sandbox (AppContainer) was reviewed only through its unit tests.
- Scoop/winget manifests need a Windows zip that the release workflow does not build yet.
- Model-facing prompts in `agent.py` are Russian; only user-visible TUI notices were localised.
