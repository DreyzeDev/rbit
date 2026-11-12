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

### Changed

- Session writes are debounced (1.2s) instead of rewriting the file on every stream
  chunk; pending writes flush on exit.
- File writes are atomic, ANSI escapes are stripped, and checkpoints rotate (20).
- Session saves no longer depend on ambient provider environment variables in tests.

### Fixed

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
