# Changelog

All notable changes to rbit are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

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
