# Security policy

## Supported versions

Only the latest `main` branch is supported with security fixes.

## Reporting a vulnerability

Please do not open a public issue for security problems. Report them privately via
GitHub Security Advisories ("Report a vulnerability") or by email to the maintainer
listed in `pyproject.toml`. Include reproduction steps and the affected version.

You can expect an acknowledgement within 72 hours and a coordinated disclosure once
a fix is available.

## Handling secrets

- rbit stores configuration, sessions, and checkpoints in `.rbit/` inside the project.
- On POSIX the config file is created with `0600` permissions.
- Secrets found in tool output and exports are redacted automatically, and `.env`
  style files are blocked unless explicitly allowed.
- Never paste API keys into issues, logs, or committed files.
