# Changelog

All notable changes to rbit are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
[Semantic Versioning](https://semver.org/).

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
