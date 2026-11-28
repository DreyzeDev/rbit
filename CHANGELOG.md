# Changelog

All notable changes to rbit are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [1.7.0] - 2026-11-28

### Security

- **Desktop app asks before running shell commands.** The backend it starts now runs with
  `RBIT_APPROVALS=1` and the window shows an approval dialog (allow once / always allow / deny,
  Esc denies, buttons are armed after a short delay). Before, the desktop ran every command
  unattended.
- **"Always allow" no longer approves a whole program.** For interpreters and launchers
  (`python`, `node`, `bash`, `sudo`, `npx`, `docker`, ...) and for `tool -option ...`, only the
  exact command is remembered, so approving `python script.py` no longer approves `python -c "..."`.
- The web UI at `/` is shipped in the wheel and sdist (it returned 404 for a pip-installed package).
- `rbit serve`: the `ask`, `review`, `debug` and `autopilot` modes are honoured (they used to be
  silently turned into `build`, i.e. full write access); a schedule created over HTTP while approvals
  are on does not run shell commands unattended; `git status`/`git diff` for `/v1/vcs/status` run with
  repository-configured programs (`core.fsmonitor`, hooks, external diff) disabled and a timeout;
  a token in the URL is accepted only to open the page (which keeps it in a `SameSite=Strict`
  cookie), and cookies are honoured only for same-origin requests; `/v1/fs` works when the
  workspace is a symlink.
- `rbit mobile` states that the connection is plain HTTP.

### Fixed

- Terminal UI: the model and prompt-history shortcuts were `ctrl+m` and `ctrl+h`, which terminals
  deliver as Enter and Backspace, so they never fired. They are now `f2` and `f3` (the old keys stay
  as aliases for terminals that can tell them apart); stop-background, collapse-cards and sidebar
  width have `f6`-`f9` next to the `ctrl+shift` combinations that most terminals cannot send.
- Terminal UI: `"bindings"` in the config file did nothing; they now rebind the action, and the
  on-screen hints follow.
- Terminal UI: on a short or narrow terminal the logo, tip and provider name give way so the prompt
  box is never cut off; the sidebar no longer claims "LSP python Ready" when nothing runs and hides
  Ollama when it is simply not installed.
- Desktop: the saved model is restored (`current` vs `active`), tool calls and their output are shown
  live and in loaded sessions, errors and a dead backend are shown instead of swallowed, Stop works
  before the session id arrives, session rows are keyboard-reachable and deleting asks first, Markdown
  lists/headings/italics/links render, Enter during IME composition no longer sends, plus focus
  outlines, `prefers-reduced-motion`, a light theme and a narrow-window layout.
- Lint: loop-variable and `zip(strict=)` findings; invalid escape sequences in tests.

- `rbit plugin add|check|update|remove <package>` was unreachable (a second `plugin` command
  shadowed it, and its handler was defined twice); the package commands are routed again, and
  `remove <name>` prefers an installed project plugin.
- Sandbox: the Linux bubblewrap launcher also protects `.git/config` and masks system credential
  files (`/etc/shadow`, ...) as the Landlock launcher does; the macOS profile protects `.git/config`;
  `rbit sandbox status/check` no longer says `shell_sandbox` is `strict` when it is not.
- Type fixes found on the way: a possible `None` call in the streaming path (`on_stream_chunk`) and a
  slash-command handler that fell off its end instead of returning `False`.

### Added

- `locale` setting (`auto` | `ru` | `en`, default `auto` from the environment): TUI notices are
  translated through `rbit.i18n` instead of being hard-coded in one language.
- `RBIT_SHELL_SANDBOX=off|auto|strict` sets the default sandbox mode; the desktop app starts its
  backend with `auto`.
- Schedules: prompt length, agent, model and count are validated (`MAX_PROMPT_CHARS`, `MAX_SCHEDULES`).
- `scripts/render_distribution.py` fills the Homebrew formula (and Scoop/winget when a Windows zip
  exists) with the real version and SHA-256; the release workflow attaches the result.

### Changed

- CI also runs Python 3.13, `ruff` (`F,E9,B,PLE`) and mypy on the whole `rbit` package (it was 2
  files); the release workflow runs them too before publishing.
- `shell_sandbox` stays `"off"` by default for the CLI; `"strict"` in `trust.py` is only the value an
  untrusted project config may *set* (it narrows access).
- `rbit serve` agent descriptions are English (API text), the mode list has all six modes.

### Added

- **OS-level sandbox for everything the agent starts (`shell_sandbox`).** `"auto"` runs
  programs inside the sandbox when the machine has one, `"strict"` runs them there or not at
  all (default `"off"`). Covered: `bash`, the persistent `terminal`, `git`/`ripgrep`/`ruff`
  that rbit runs itself, project formatters, language servers (LSP) and MCP servers. Inside,
  a program can read and write the workspace and a private scratch folder, read the system
  and the interpreter, and nothing else: not the home directory (`~/.ssh`, `~/.aws`,
  `~/.rbit` with the API key), no network (not even loopback), no sight of other processes,
  no way to gain privileges, and an environment cut down to an allowlist (a key nobody
  thought about is not inherited). Settings: `shell_sandbox_network`, `shell_sandbox_read`,
  `shell_sandbox_write`, `shell_sandbox_env`, and a per-server `"sandbox": {...}` block for
  MCP servers. Python plugins run inside rbit itself, which no OS sandbox can confine, so
  under `strict` they are not loaded.
  - **Linux:** a built-in launcher (no extra software): user/mount/pid/net namespaces, a
    private `/proc`, Landlock, seccomp (no `ptrace`, `mount`, `unshare`, `bpf`, `io_uring`,
    nested namespaces...), `no_new_privs`, empty capabilities. It never runs the command
    unconfined if a step fails. bubblewrap is the fallback.
  - **Windows:** AppContainer plus a Job Object (process-tree kill, process cap, no clipboard
    or desktop access, only the three standard handles inherited). Access is granted through
    ACL entries recorded in `~/.rbit/sandbox-grants.json`; `rbit sandbox cleanup` removes
    them. The project's own `.rbit` folder is closed to the sandbox, PowerShell runs on a
    workspace-rooted drive, and `shell_sandbox_network` adds the `internetClient` capability.
  - **macOS:** `sandbox-exec` with a generated profile.
  - `rbit sandbox status` and `rbit sandbox check` report the backend and *measure* the
    isolation on this machine (12-14 checks that start real programs: what must work works,
    what must be denied is denied). The test-suite runs the same checks.
  - A project's `.rbit/config.json` can switch the sandbox on but not off or wider (see
    `rbit trust`).
  - **Audited and verified on:** Windows 11 (AppContainer, 15/15 checks) and Linux (WSL2
    kernel 6.18, 22/22 checks as `nobody` and as root). **Not run on macOS**: the profile is
    covered by tests of the generated text only and it leaves most of the home directory
    readable. A kernel or OS vulnerability can defeat any sandbox; DPAPI is not a boundary on
    Windows. See `docs/security/sandbox-audit.md` (findings, fixes, re-test evidence) and
    `docs/security/macos-verification.md` (how to check a Mac).
  - **Fixed after two independent red-team audits:** unix sockets outside the workspace
    (ssh-agent, docker, D-Bus, the WSL bridge) are no longer reachable on Linux (the launcher
    builds a new root with `pivot_root`; abstract sockets, `AF_VSOCK` and more are blocked);
    `/etc/shadow`, `.rbit` and `.git/hooks` are masked or read-only; an MCP entry's `cwd` can no
    longer become the sandbox workspace; folders that are or contain `~/.ssh` and friends are
    never granted; `rbit trust` now shows an MCP entry's `cwd`, `sandbox.*` and variable names;
    on Windows hard-linked files are not opened through a workspace grant, each set of extra
    folders gets its own container identity, `.git/hooks` is read-only, the grants ledger tracks
    folder identity, the job has a memory limit, and programs run on a private desktop.
  - Separate from the older `bash_sandbox`, which is a best-effort text check for path
    escapes in the command.

### Security

- **A cloned project's `.rbit/config.json` can no longer run programs or widen permissions
  on its own.** MCP/LSP servers, plugins, hooks, formatters, `permission_mode`,
  `auto_approve`, `allow` permission rules, `allow_outside_paths`, `allow_secret_reads`,
  `instructions`, `api_base` (where the API key is sent) and similar settings are ignored
  until you trust them with `rbit trust` (`--status`, `--yes`, `--revoke`). Trust is kept
  per workspace and per setting in `~/.rbit/trust.json`, as a fingerprint, so a changed
  value must be trusted again. Settings you change yourself (`mcp add`, `/connect`,
  `plugin enable`, the permissions dialog) are trusted automatically; `deny`/`ask` rules
  and everything harmless keep working. `RBIT_TRUST_ALL=1` restores the old behaviour.

- **`bash` fails closed when nobody can approve a command.** Unlike `terminal`, it ran any
  command if the executor had no approval channel. Now such a command needs a standing
  approval: `rbit run`, ACP and `--auto` already set one; otherwise a `shell` allow rule or
  the `commands`/`auto` permission mode. Read-only commands are unaffected. Visible effect:
  `rbit watch --run` no longer lets the agent run mutating shell commands unless the project
  is configured to allow them.

- **The daily budget binds every client, not only the TUI.** `daily_budget_usd`,
  `model_budgets` and `daily_token_limit` (with `budget_mode: strict`) were checked only in
  the TUI, and only the TUI booked spend in the usage ledger, so `rbit run`, `serve`, ACP and
  `watch` ignored the cap and `/usage` under-counted. The engine now always uses the
  workspace ledger and checks the guard before a turn.

- **Child processes no longer inherit the agent's LLM API keys everywhere.** Only `bash`
  stripped them; the interactive `terminal` tool, project formatters, `git`, `ripgrep` and
  `ruff` started with the full environment, so `echo $OPENAI_API_KEY` in `terminal` (or a
  hostile repo's formatter/git config) could read the key. They now all use
  `rbit/envscrub.py`, whose list also covers more providers (Perplexity, Fireworks,
  Cerebras, NVIDIA, Replicate, SambaNova).

### Fixed

- **`grep` finds text in legacy-encoded files.** With ripgrep installed, a Cyrillic (or any
  non-ASCII) pattern silently skipped cp1251/cp1252 files, because ripgrep reads UTF-8 only.
  Non-ASCII patterns now use the built-in search, which decodes each file the same way
  `read` does; ASCII patterns still use ripgrep.
- **Files that are not UTF-8 can be edited.** `edit` read such a file as UTF-8 with
  escapes, so the model's text (real Cyrillic) never matched the bytes. The file's
  encoding is now detected (UTF-8, UTF-8/16/32 with BOM, otherwise cp1251 for Cyrillic
  text or the locale's / cp1252 code page), `read`, `grep` and `symbols` show decoded
  text, and `edit`, `multi_edit`, `apply_patch` and `write` keep the file's encoding
  and line endings. Untouched bytes always round-trip exactly; characters the code page
  cannot hold (an emoji in a cp1251 file) are refused with a clear message instead of
  corrupting the file. New files are still UTF-8.
- **DNS rebinding in `webfetch` / image downloads.** The host was resolved for the SSRF
  check and then again by httpx when connecting, so a DNS server could answer a public
  address first and `127.0.0.1` second. Requests now connect to the address that was
  validated (the `Host` header and TLS name stay the original host).

- **`model_router` was never used.** The smart router (tiers, pin, policy) and its
  `never_switch_mid_task` guard existed but nothing called them. When
  `model_router.enabled` is set, `run_turn` now routes the request (announced as a
  `router: <model> [<tier>] — <reason>` status line). With `never_switch_mid_task`
  (default on) the model only changes on a session's first request or on an
  explicit pin. A disabled router leaves the selected model alone, and the older
  `model_routing` (light/heavy) is skipped while the smart router is active.

- **Bug hunt (core, CLI, TUI).**
  - Linux: the persistent terminal no longer echoes every typed line twice (the shell now runs
    without a forced `-i`/readline pair).
  - `lint` fallback is only "clean" when the checker really reported `0 issues`.
  - `rbit serve` liveness ignores zombies and processes that merely mention `serve`.
  - Toast notifications show `[brackets]` literally instead of swallowing them as markup.
  - The diff viewer survives diffs that contain Markdown code fences.
  - `set_setting` (used by `pair` and plugin install) backs up an unreadable `config.json`
    instead of replacing it with a one-key file.
  - `sessions prune 1M` means one month (it was one minute); `config set auto_approve no`
    stores `false`, and a text `"no"`/`"off"`/`"0"` no longer switches confirmations off.
  - OpenAI/HTML exports and saved sessions mask secrets (shell commands, diffs, messages)
    the way the Markdown export already did.
  - Rewriting a whole file keeps its UTF-8 BOM; untrusted-content fences cannot be closed by
    the content itself; `todowrite` accepts plain-string items; SSE events with U+2028/U+0085
    inside JSON are no longer cut in two; `_strip_blocks` no longer drifts on `İ`.
  - Reasoning tokens are no longer billed twice in cost estimates.
  - `/retry` resends the user's own prompt, not the app's `[auto]` self-check follow-up; the
    debounced session save is flushed before the session lock is released.

- **Undo and session fixes.** A refused `/undo` (file edited after rbit's change) no longer
  throws the snapshot away; `/undoturn` skips a conflicting file, restores the rest and says
  how many it skipped; `/new` forgets the previous session's undo history; `/clear` is saved;
  `!cmd` typed while the agent is busy stays in the input; `/cost` shows no literal `**`.
- **Smaller fixes.** `read` and `grep` count lines like editors do (form feed, U+2028 and U+0085
  no longer shift line numbers); project memory no longer glues a bullet onto an unterminated
  line or drops "use pytest" because "use pytest -x" exists; `/usage` groups by local day.
- **Command palette** gains "Undo every file edit of the last turn" and "Show estimated cost".

## [1.6.0] - 2026-11-18

Closes every remaining actionable item in `docs/RBIT_BLUEPRINT.md`: 132 of 134
done, 2 (desktop control, cloud/team) deliberately out of scope.

### Fixed

- **The answer appeared to be missing after tool activity.** A model normally
  writes a sentence before calling a tool, which mounts the live answer card
  first; tool cards were then appended at the *end* of the log, i.e. after the
  answer. After a dozen reads the reply sat far above them, so scrolling to the
  bottom showed only tool cards and the end-of-turn file recap. Tool cards are
  now inserted above the live answer card, so the reply always grows at the
  bottom of the log. Verified by removing the fix: the regression test fails, and
  the on-screen check loses both the start and the end of the answer.

### Added

- **Hook limits** (`hooks.timeout_s`, output cap, per-session call budget). Hooks
  are third-party code run on every tool call, so a hung one froze the turn.
- **Plugin approval gate.** `rbit plugin install` leaves a plugin *disabled*;
  `rbit plugin enable <name>` opts in. Code that arrives inside a cloned repo
  therefore cannot run just because the repository was opened.
- **MCP tool risk classes** (`read` / `write` / `destructive`) derived from server
  annotations or the tool name, enforced through the permission engine. A
  destructive tool with no rule and no confirmation channel is refused rather
  than guessed at.
- **Subagent budget caps** (`subagent_max_steps`, `subagent_max_cost_usd`). A
  nested agent loop no longer spends invisibly.
- **Router policies** `cheapest` and `best_value` with `max_cost_per_1k`, plus
  `check_capabilities()` verifying tool calling and context size before a model
  is switched to.
- **`rbit selftest`** — post-upgrade verification against a throwaway workspace
  (imports, tool schema, storage round-trip, permission engine, injection guard,
  external tooling, config). `--json` for CI.
- **Dry-run** (`rbit.selftest.simulate`) reporting what a tool call would do
  without performing it.
- **`rbit skill install/list/verify`** — install from a Git URL or local path,
  record the origin and a content digest, refuse to clobber a locally modified
  install without `--force`, and report drift.
- **`rbit plugin list/install/enable/disable/remove`** with a validated manifest.
- **`rbit migrate`** — config upgrades (`budget_usd` → `daily_budget_usd` +
  `budget_mode`, `auto_approve` → `permission_mode`, explicit safety defaults)
  with a timestamped backup and a `--dry-run`.
- **`rbit diag`** — diagnostic bundle (environment, redacted config, instruction
  sources, self-test, recent actions) for bug reports.

### Notes

- `hooks.require_approval` defaults to `false` because a config-listed plugin was
  placed there by the user; set it to `true` to gate every hook by name.
- MCP manifest and config reads accept a UTF-8 BOM, which Windows editors write by
  default.

## [1.5.0] - 2026-11-18

Implements the CLI Feature Blueprint (`docs/RBIT_BLUEPRINT.md`), renamed from
DreyzeCode to rbit. Every item below is covered by tests.

### Added

- **`RBIT.md` project instructions.** `RBIT.md` is now the first instruction file
  rbit looks for, ahead of `AGENTS.md` and `CLAUDE.md`, which stay supported for
  compatibility. `discover_instruction_sources()` reports *which* files are
  active, and `/status` and `/doctor` show them — silently loaded rules are
  impossible to debug.
- **Audit log** (`rbit audit`). Append-only JSON Lines in
  `.rbit/audit/YYYY-MM-DD.jsonl` recording every tool call and permission
  decision: tool, arguments, outcome, summary, duration, session, agent mode.
  Secrets are redacted at every nesting depth before writing. Actions:
  `summary`, `tail`, `path`, `clear`, with `--day` and `--json`.
- **Usage ledger and Budget Guard** (`rbit usage`). Per-call accounting of
  input/output/reasoning/cached tokens and cost in
  `.rbit/usage/YYYY-MM-DD.jsonl`, with daily `daily_budget_usd` /
  `daily_token_limit`, per-model `model_budgets`, thresholds at 50/75/90/100%,
  a burn-rate forecast, and JSON/CSV export. `budget_mode: strict` stops a paid
  call *before* it happens; concurrent calls are counted via reservations so
  parallel requests cannot jointly overshoot the cap. Cost is never invented —
  an unknown price marks the record `priced: false` instead of guessing.
- **Smart model router** (`rbit/router.py`). Classifies a task into
  `fast` / `balanced` / `deep` and picks the configured model per tier. The
  decision always carries a reason (`RoutingDecision.describe()`), a user pin
  wins, an unavailable model falls back to the current one, and
  `never_switch_mid_task` keeps the model stable during a task.
- **Prompt-injection guard** (`rbit/guard.py`, `prompt_injection_guard`). Web
  pages and search results are fenced in `<<<UNTRUSTED_CONTENT>>>` markers with an
  explicit "this is data, not instructions" warning; high-severity findings add a
  security notice naming them. Modes `off` / `warn` (default) / `strict`, where
  strict refuses the content outright. Detection covers instruction overrides,
  role changes, system-prompt and secret exfiltration, tool coercion, fake
  system blocks and hidden Unicode. Findings are audited.
- **Guarded `git bisect`** in the `git` tool: `status`, `start`, `good`, `bad`,
  `skip`, `reset`, `log`. Mutating subactions go through the permission engine.
  `git bisect run` is refused by design — it would execute an arbitrary command
  for every step.
- **`/plan`, `/execute`, `/bug`.** `/plan` forces Plan mode (where the mutating
  tools are disabled, so nothing can be written by accident) and asks for a plan
  plus todo. `/execute` returns to Build mode and runs the plan, reusing the last
  assistant message when no task is given. `/bug` drives a reproduce → isolate
  (bisect) → prove-with-a-test workflow.
- `docs/RBIT_BLUEPRINT.md`: the full checklist with per-item status.

### Fixed

- Audit redaction now recurses into nested values. Tool arguments arrive as a
  dict, so redacting only top-level strings let
  `{"command": "export API_KEY=..."}` through to disk.

## [1.4.4] - 2026-11-18

### Fixed

- **The answer could still end up below the fold.** `pin_to_end()` respects
  `stuck_to_bottom` so it never fights a user who scrolled up — but at the end of
  a turn that is exactly wrong: the user scrolled up *while tools were running*,
  and the answer they asked for must be shown. Revealing used the timid path, so
  it silently did nothing. `ChatScroll.reveal_tail()` scrolls unconditionally and
  `_reveal_turn_result()` uses it.
- **The reveal gave up too early.** It re-pinned on three fixed timers
  (0.08/0.25/0.6 s). On a long session (measured: 533 widgets, 12 384 virtual
  rows) the reply card takes far more frames to reach its final height
  (compose → Markdown render → reflow of tables and fences), so every pin fired
  before the growth and the tail stayed hidden. The reveal now repeats after each
  refresh until the content height stops changing, bounded by a 3 s deadline.
- `ChatScroll.watch_scroll_y` no longer mistakes a pin for a user scroll when the
  pin target is 0 (an empty or short log), which used to keep auto-follow on after
  the user had scrolled to the top.
- `RbitApp._sync_ui()` returns early once the app is no longer running, so a turn
  worker finishing during quit/teardown cannot crash on `NoMatches`.

## [1.4.3] - 2026-11-18

### Fixed

- **A slash command needed two Enters.** Typing `/effort` and pressing Enter only
  rewrote the input to `/effort ` and did nothing; the menu opened on the second
  Enter. The completion handler treated a slash command like a file path — a
  fragment to be spliced into a longer prompt — even though a slash command is a
  complete action. Enter now resolves the command and runs it immediately; a
  partial `/eff` picks the highlighted match and acts. A bare `/` stays inert so
  it cannot launch whatever happens to sort first, arguments
  (`/permissions auto`) survive, and `@file` completion still fills in without
  submitting, because a path really is only part of a prompt.

## [1.4.2] - 2026-11-18

### Fixed

- **The answer was invisible after tool activity.** A turn could finish with
  every tool card on screen and the model's reply nowhere in sight. Three
  separate causes, all on the same path:
  - `AssistantMessageCard.finalize()` did `query_one(Markdown)` inside a bare
    `except NoMatches: return`. When the live card was mounted by the *last*
    stream chunk it had not composed its children yet, so the final answer was
    silently dropped. It now retries once the children exist.
  - Nothing brought the user back to the reply when a turn completed. Auto-follow
    during a turn is deliberately timid so it cannot fight manual scrolling, but
    the end of a turn is exactly when the answer matters. `_reveal_turn_result()`
    re-attaches and pins after the final reflow — with repeated pins, because the
    card is mounted without awaiting and lands a frame or two later.
  - Submitting a new prompt now re-engages following (`pin_to_end(force=True)`):
    asking for something means wanting to watch it.
  - `ChatScroll.watch_scroll_y` no longer treats a programmatic pin as a user
    scroll. A pin lands on an offset that was current when it was issued while
    the content has usually grown since; judging that as "the user scrolled up"
    silently disabled follow for the rest of the turn.

## [1.4.1] - 2026-11-18

### Fixed

- **Streamed answers were cut off below the fold.** `scroll_end()` resolves
  against the *previous* maximum scroll offset, while mounting a card or growing
  a `Markdown` widget only reflows on the following frame. With a 20-chunk
  stream the viewport lagged 16 rows behind the content, so the beginning of a
  reply was visible but its tail was hidden. `ChatScroll.pin_to_end()` re-issues
  the scroll after each refresh and retries across the reflow frames; the live
  card and `finalize()` now re-pin after `Markdown.update`. The four direct
  `scroll_end()` call sites in the app were switched to the same path. Pinning
  still yields to the user: scrolling up clears `stuck_to_bottom` and stops it.

## [1.4.0] - 2026-11-18

### Fixed

- **TUI responsiveness.** Removed the blocking work that made the interface
  freeze for hundreds of milliseconds at a time:
  - `StorageManager.list_checkpoints()` re-read and JSON-parsed every
    checkpoint (each embeds full file contents) on every `_sync_ui()` — 38 call
    sites in the TUI. Measured **43.8 ms per refresh for 20 checkpoints
    (24 MB)**, **325 ms** for the 15 real checkpoints in a home workspace.
    Checkpoint index is now memoised against the directory mtime and the
    sidebar uses `checkpoint_count()`, which only counts directory entries:
    **0.026 ms (1665x faster)**.
  - `detect_git_branch()` ran `git rev-parse` synchronously inside
    `RbitApp.__init__`, freezing the first frame up to the 1.2 s timeout.
    It now resolves on a background worker thread.
  - Unfinished-session recovery called `list_sessions()` during `on_mount`,
    which parses every session file — **257 ms** of frozen UI on a
    home-directory workspace. Moved to a worker with the result applied via
    `call_from_thread`, and guarded against teardown (`NoMatches`).
  - `@file` autocomplete walked the whole workspace on every keystroke
    (`os.walk` over up to 2000 entries). Now debounced by 120 ms.
  - Session saves (`save_session()`) serialised the entire session and hit
    disk on the UI thread right after each streamed chunk. Now written on a
    dedicated worker thread.

### Added

- **Event-loop lag watchdog.** The 90 ms spinner tick doubles as a heartbeat:
  late ticks record how long the UI thread was blocked. Exposed as
  `RbitApp.ui_lag_stats`, shown in `/doctor` (`UI Lag: worst N ms, avg N ms`)
  and announced with an opt-in notification via `warn_on_ui_lag` in the config.
- `tests/test_ui_responsiveness.py` pinning all five fixes (checkpoint cache
  invalidation, no subprocess in the constructor, completion debouncing,
  cache-tolerant counting, lag recording).

## [Unreleased]

### Added

- Native SwiftUI iPhone client in `ios/` with liquid-glass tab bar, rbit theme,
  streaming NDJSON chat, tool cards, model/effort/agent pickers, avatar, themes
  and settings. Server side: `rbit mobile` LAN mode with a stable token,
  `rbit://connect` pairing links, optional QR output (`rbit[mobile]`), richer
  `/v1/status`, `/v1/agents`, `/v1/sessions/{id}` payloads, session abort,
  bearer/pair token auth and CI build for an unsigned IPA.
- Agent parity additions: `question` tool (TUI modal, clear non-interactive
  fallback), `artifact` storage/tool, `schedule` tool + `rbit schedule`
  (`list/add/remove/run-due`), auto-compaction for headless runs, model variants
  (`provider/model#low|medium|high`), and multi-provider websearch
  (Exa/Tavily/Firecrawl with DuckDuckGo fallback).
- Tools for terminal sessions (`terminal`), image generation (`image_generate`) and artifacts/schedules, plus model variants and multi-provider websearch.
- Cron-style schedules, schedule runner lock, MCP resource list/read tool, ACP session replay, terminal/image tools.
- Built-in `rbit-design` skill (with theme.css/liquid-nav.js/sheet.js/stickers.js references) (bundled, project/global overrides) with automatic activation when a UI request has no visual direction, plus opt-out `auto_design_skill`.
- LSP rename + document symbols, budget warnings/strict mode, `/editor` alias, PDF reads (extra `pdf`), `rbit test --changed`, `rbit gh issue|pr`, optional Playwright `browser` tool.
- OpenCode Go provider: `opencode-go/` model prefix, `x-opencode-session`, Go model catalog, verified live with Go subscription.
- Hook lifecycle (`before_edit`, `after_edit`, `before_command`, `after_command`, `on_error`, `on_stop`, `session_start`) and `git branch` action.\n- Blueprint batch: `read_many_files`, LSP definition/references/hover tool, `budget_usd` guard, `/cost`, `/usage`, `/status`, `/permissions`, `/agents`, `/provider`, `/hooks`.
- `rbit status` dashboard, `rbit config schema`, `rbit models --json`.
- Fast `rbit --version` and `--generate-completion` paths (~50ms), global `--no-color`, Windows/macOS smoke CI.
- CLI quality: `rbit config get/set/list/validate/edit`, `rbit permissions list/explain`, `rbit completion install/print`, `rbit logs`, global `--log-file`, mypy + CLI smoke in CI, pyproject metadata/keyring extra.
- Change-risk analyzer (`rbit risk`, `risk_review` tool) and session replay (`rbit replay`) with deterministic git-diff scoring.
- Server API bearer-token auth (`RBIT_SERVER_TOKEN`/service-generated token), pair-cookie flow for the web UI, 0600 service state, and keyring-backed multi-credentials.
- Native Google Gemini generateContent support (`protocol: gemini`) with functionCall/functionResponse loops.
- Native Anthropic Messages API support (`protocol: anthropic`) with tool_use/tool_result loop.
- Server API additions: `/v1/artifacts`, `/v1/schedules`, `/v1/fs`, `/v1/usage`,
  `/v1/config`, `/v1/skills`, `/v1/vcs/status`, `/v1/reload`, plus OpenAPI IDs.
- ACP: session modes (`build`/`plan`), per-session model selection, auto-approved
  tool permissions for editor clients.
- CLI parity: background `rbit service` (start/stop/status/restart/get/set/unset),
  OpenAPI-driven `rbit api`, `rbit debug` (agents/config/paths/heap-dump), package
  plugins (`rbit plugin add/list/check/update/remove`), multi-credential auth
  (`switch/export/import`), MCP OAuth token management (`mcp auth/logout`),
  `rbit upgrade`/`uninstall`/`reload`/`pair`/`mini`, `serve --cors/--service/--stdio`
  and `POST /v1/reload`.
- `rbit run` additions: `--worktree`, custom `--agent`, `--archived`, `--thinking`;
  global `--auto/--yolo` permission auto-approval; archived sessions are hidden from
  lists but still resumable.
- `rbit sessions --max-count/--json/--all`, `session import` from URL, richer `stats`
  (`--year/--tools/--cost/--full/--json/--limit`).
- Full MCP client: stdio + streamable HTTP servers from `mcp` (opencode-style) or
  legacy `mcp_servers`, `<server>_<tool>` namespacing, `/mcp add|remove|reload`,
  `rbit mcp list|test|add|remove`, live sidebar status.
- Permissions policy: ordered `permissions` rules (`allow|ask|deny`, last match wins,
  wildcards), legacy `permission` map, per-agent rules, `external_directory` boundary,
  interactive "always allow" memory, shell deny-before-execute.
- Project instructions: hierarchical `AGENTS.md`/`CLAUDE.md`, global rules,
  `instructions` globs and remote URLs (cached), injected as `<rules>` blocks.
- Opt-in formatters (`formatter: true|{...}`): ~25 built-ins plus custom commands,
  environment/extensions overrides, auto-run after write/edit/multi_edit/apply_patch.
- LSP client: pyright/pylsp, TypeScript, gopls, rust-analyzer, clangd, bash/yaml/json/
  css/html, zls, terraform-ls, lua, php, ruby, swift and custom servers; diagnostics
  tool uses LSP when `lsp` is enabled, with built-in syntax fallback.
- Plugin hooks: local `plugins` modules plus entry points; `tool.execute.before/after`,
  `chat.params`, and `event` hooks with mutable contexts.
- Skills: model-callable `skill` tool, `.rbit/skills/*.md` and `*/SKILL.md`, global
  skills, frontmatter descriptions, `{args}` substitution, prompt listing.
- Subagents: custom `.rbit/agents/*.md` (frontmatter tools/mode), read-only by default,
  `mode: build` opt-in, background runs, available-agent listing.
- `websearch` tool (DuckDuckGo HTML/lite, result parsing, cache, config switch) and
  URL/line-range attachments (`@file#L12-L34`).
- `rbit acp`: Agent Client Protocol server over stdio (initialize, session/new,
  session/prompt streaming chunks, cancel) for Zed-style editors.
- `rbit serve` now exposes OpenAPI at `/openapi.json`.
- Second-echelon features: `rbit watch` (file watcher with optional agent reaction),
  macro replay (`/macro`, `.rbit/macros/*.json`), `/mcp` server config manager,
  LSP-server detection in the sidebar/doctor, plugin loading (`rbit.plugins` entry
  points), config-driven keybindings (`bindings`), minimal ru/en localization
  (`i18n`, `locale` config), monorepo extra roots (`extra_workspaces`), voice input
  (`rbit voice`, optional sounddevice+whisper), pinned messages, prompt-history
  search (ctrl+h), /test /review /explain /deps, turn latency in the header,
  quiet mode, session lock, first-run doctor hint, OS notification on long turns,
  tool-output auto-truncation marker, move detection in diffs, /undoturn,
  HTML/OpenAI session export+import, SQLite storage backend, session schema
  migrations, encrypted sessions (Fernet, optional), `rbit backup`/`describe`,
  NO_COLOR support, `rbit stats` LOC/language/complexity metrics.

- `rbit serve` — local HTTP endpoint: `POST /v1/turns` streams a full agent turn as
  NDJSON (status/thinking/delta/tool_call/message), `GET /health` for readiness.
- opencode Console/Zen provider: models are discovered from
  `https://opencode.ai/inference/v1/models` (base `.../inference/openai/v1`), and the
  catalog schema (`limit.context`, `cost.text.*`, display names) is parsed. Store it as
  a config profile (e.g. `profiles.opencode`) and run `rbit --profile opencode`.
  Free `*-free` models are limited to the OpenCode client itself; paid models need credits.
- `rbit backup` — zips the `.rbit` state directory; `rbit describe` — project
  structure, languages and README head.
- `rbit run --readonly` (and `readonly` config): read-only session — mutating tools
  and bash are rejected; `rbit run --prompt-profile fast|careful|reviewer|docs`.
- Prompt profiles: `fast` (terse), `careful` (defensive), `reviewer` (read-only,
  mutating tools rejected), `docs` (no bash/subagent).
- Turn planner: build-mode prompts ask for a 1-3 line plan before the first tool call;
  the first plan line appears in the status line.
- Self-check: after a turn that mutated files, diagnostics run over them; a single
  automatic fix round is dispatched when issues are found (`self_check` config).
- Prompt caching: system message carries `cache_control: ephemeral` for
  OpenRouter/Anthropic bases (`prompt_caching: auto|on|off`).
- Native Ollama `/api/chat` path (NDJSON streaming, `thinking`, dict tool arguments)
  when the base points at :11434.
- `model_routing` (light/heavy) by task size, attachments and keywords;
  `model_capabilities` overrides the tools heuristic (gemma/phi/tiny families get no
  tools in the payload).
- Provider model listings parse `context_length` and OpenRouter-style per-token
  pricing into `cost_per_1k`.
- `notebook` tool: cell-level read/set/insert/delete for `.ipynb` with undo snapshots.
- Cost estimate in the assistant card meta (tokens × provider price).
- Phrase-based mode switch: prompts starting with "спланируй/составь план/plan:" run
  in Plan mode, "делай/исправь/implement:" in Build mode.
- Distribution: Dockerfile + ghcr workflow, release + PyPI publish workflow, composite
  GitHub Action (`action.yml`), Scoop/winget/Homebrew manifests, MkDocs site,
  vhs demo tape (`demo/demo.tape`).

### Changed

- bash sandbox (`bash_sandbox` config): best-effort path restriction to the workspace.
- `rbit/stats` items and the full IMPROVEMENTS.md backlog are closed (76 → 100 items).
- Prompt queue: a prompt submitted while the agent is working is queued (preview in
  the status line) and runs automatically when the current turn finishes; Esc clears
  the queue.
- Tool-argument streaming feedback: while the model streams tool-call arguments, the
  status line shows `стрим аргументов: write · 1.2 KB` (on name change or ~512 bytes).
- Sidebar "Modified Files" entries are clickable and open a unified diff modal
  (session start vs current file state).

- Model fallback to `rbit/smart-local` on HTTP 401/404 responses.
- `rbit run --stdin`, `--output-format jsonl`, and `rbit commit` (`--apply`).
- Shell completion via `rbit --generate-completion bash|zsh|powershell`.
- Config profiles with `--profile <name>` overlays from `config.json`.
- `F1` help screen, Ollama sidebar status, high-contrast theme, compact mode.
- Inline `Thinking...` spinner in the chat feed, shown only while a model
  request is in flight. Click it (or the collapsed `Thought for Ns` line) to
  reveal the model's reasoning in gray; reasoning deltas are streamed from the
  provider via `on_reasoning_chunk`.
- `bash` timeout argument and background execution.
- Readable Markdown `webfetch` output with a 10-minute cache.
- ripgrep-backed `grep` when `rg` is available.
- Custom provider headers via `extra_headers`.
- `/connect` imports every model from the provider's `GET /models` under its own
  category in the model picker and caches them in the config.
- Automatic model discovery: cached provider models load immediately, refresh in the
  background at startup, on `/connect`, and whenever the model picker opens.
- Reasoning effort selection (`/effort`, `--effort`, picker modal) for o-series,
  DeepSeek R, QwQ, and other thinking models via `reasoning_effort`.
- `apply_patch` tool: applies multi-hunk unified diffs with context validation and can
  create files.
- Background `bash` processes can be stopped from their card, with `Ctrl+X` as a
  stop-all shortcut.
- Session search via `Ctrl+F` / `/search` across messages and tool output.
- `rbit update [--check] [--json]` checks PyPI and upgrades the installed package.
- Agent `todowrite` updates now refresh the sidebar and todo manager immediately.
- Partial assistant answers are saved with an `interrupted` marker when `Esc` cancels a turn.
- Tool cards expand with `Enter`; `Ctrl+E` collapses every expanded card.
- Chat scroll sticks to the bottom only while the user is already there.
- Workspace path validation rejects reads/writes outside the project unless
  `--allow-outside` or `allow_outside_paths` is set.
- Turn budget controls: `max_steps` caps tool-calling rounds and `max_cost_usd`
  stops the turn when the estimated spend is exceeded.
- Crash recovery: an interrupted turn marks the session `finished: false` and
  reloads it automatically on next launch.
- History renders in windows of 30 cards with a "load older" link for long sessions.
- Right-click a tool card copies its path, command, or pattern; `Ctrl+R` retries
  the last prompt and error notices hint at `/connect` for auth failures.
- Colored diff cards: `+` lines are green and `-` lines red with a dimmed line-number
  gutter, and `(+N -M)` counters in card headers are tinted to match.
- Fenced code blocks in assistant replies render with a dark panel and syntax
  highlighting; inline commands and tool output have distinct styling.
- Consistent tool-card labels for Read/Write/Edit/Patch/Bash/Glob/Grep/WebFetch and
  Symbols/Diagnostics/Lint/Git/Subagent, and command output is prefixed by a `│` gutter.

### Changed

- Bash output is dimmed and gutter-prefixed so commands stand out from their results.
- The `шаг N/M` counter was removed from the status bar; the agent loop now runs up to
  50 tool rounds by default (`max_steps` still overrides it).
- Visual polish toward a flatter, opencode-like layout: rounded prompt border, compact
  single-line session header, transparent user messages with an accent rail, slimmer
  sidebar showing the workspace name, and subtle gray scrollbars.
- Restored messages no longer show a stale `streaming...` suffix; it is reserved for
  the live card and replaced by the duration once the turn finishes.

- Session writes are debounced (1.2s) instead of rewriting the file on every stream
  chunk; pending writes flush on exit.
- File writes are atomic, ANSI escapes are stripped, and checkpoints rotate (20).
- Session saves no longer depend on ambient provider environment variables in tests.

### Fixed

- Saving the config while a profile is active (`--profile` / `RBIT_PROFILE`) now updates
  that profile instead of flattening its values into the root config and silently
  switching the default provider.
- Pasting large/multi-line text into the prompt no longer truncates it to the first
  line: the whole paste is inserted (lines joined with spaces), from both the terminal
  bracketed-paste path and `ctrl+v`.
- A stream that breaks mid-tool-call no longer glues its partial deltas onto the
  retried response (accumulators reset per attempt; `index: null` and missing
  `index`/`id` deltas no longer merge parallel tool calls into one).
- An exception inside a tool now becomes a failed tool result for the model instead of
  killing the whole turn and leaving the tool card stuck on "running".
- The non-stream step (CLI path) and the explore subagent no longer crash on malformed
  provider responses (HTML garbage, empty `choices`); tool calls without an `id` get
  one generated. `finish_reason: length` marks truncated tool-call arguments instead of
  running the tool with silently empty arguments.
- Multi-step turns keep the text of every step: the live card and the final message no
  longer lose intermediate narration, and `/search` + `/export` see the full reply.
- `!command`, `/compact`, `/commit`, new/clear/switch/fork session and `/todo` during an
  active turn are rejected with a notice instead of silently killing the turn or
  corrupting sessions; the turn also binds to its session, Esc cancels only the busy
  worker (not model discovery), and the rebuild path drops stale live-widget references.
- `@`-completion of files no longer crashes on Windows paths (bad escape `\z`) and no
  longer injects TAB/CR into the prompt; Enter now accepts the highlighted file
  suggestion too.
- Session/config writes are atomic (temp + `os.replace`), save failures surface a
  notification, and corrupted checkpoints return "not found" instead of crashing
  `/rewind`.
- Plan mode no longer treats `echo x; <mutation>` as read-only: every statement of a
  bash command must be whitelisted and output redirects force a permission prompt;
  `grep`/`findstr`/`head`/`tail`/`wc` join the read-only whitelist.
- `apply_patch` accepts patches whose empty context lines lost their leading space
  (the usual LLM form); `multi_edit` reports ambiguous matches instead of silently
  editing the first one.
- ripgrep `grep` searches hidden files/dirs for parity with the fallback (excepting
  VCS/tooling dirs), and `prod.env`, `.envrc`, `secrets.yaml`/`secrets.yml` are treated
  as secret files.
- Streaming no longer re-renders the whole Markdown document on every SSE token
  (updates coalesced to ~10 Hz); app bindings moved off keys captured by the focused
  input (`ctrl+k` removed — palette stays on `ctrl+p`, stop-background is
  `ctrl+shift+x`, collapse-cards is `ctrl+shift+e`).
- File writes preserve the target's dominant line ending: editing an LF file no longer
  rewrites every line to CRLF (and vice versa), so diffs stay minimal and follow-up
  edits stop failing with `oldString not found`.
- Fuzzy edit no longer eats the newline after the replaced block, which glued the next
  line onto the replacement and silently corrupted files.
- `restore_checkpoint` no longer deletes binary or undecodable files created after the
  checkpoint (only restorable text files are removed); a path manifest also protects
  files that existed but could not be snapshotted.
- The ripgrep-backed `grep` decodes output as UTF-8 (it crashed on non-ASCII content
  under the cp1252 locale) and reports rg failures instead of pretending "no matches";
  invalid regexes return a clear error from the fallback path.
- Streaming text no longer aborts the turn with a bogus connection error when the chat
  card has not finished mounting yet (`No nodes match 'Markdown'`).
- Provider failures are no longer reported as network errors; non-HTTP exceptions surface
  as "Ошибка обработки ответа модели" instead of "Не удалось подключиться".
- Reasoning-only replies (thinking models) are used as the assistant message instead of
  showing "(пустой ответ модели)".
- The system prompt now states the real shell (`PowerShell` on Windows) so models stop
  emitting Unix-only commands such as `ls -la`.
- Continuing a session now replays previous tool calls and their results to the model.
  Previously only plain user/assistant text was sent, so the model forgot files it had
  read, diffs it produced, and commands it had run in earlier turns.
- Clicking a tool card no longer steals keyboard focus from the prompt input; `Enter` on an
  empty prompt toggles the newest card instead.
- `rbit --allow-outside run ...` is parsed before the subcommand instead of treating `run`
  as a workspace directory.
- Interrupting a turn can no longer append both the partial and the completed answer.
- Path checks resolve absolute and `..` paths before deciding whether they escape the workspace.

### Security

- Dangerous bash deny-patterns, workspace secret redaction, and config permissions.

## [0.1.0]

- Initial public release: dual-mode agent (Build/Plan), Smart Local mode, TUI,
  sessions, checkpoints, MCP/LSP panels, Todo manager, and CLI subcommands.
