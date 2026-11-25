# Security Policy & Responsible Disclosure

## Supported Versions

Only the latest `main` branch and current release tags are supported with security fixes.

## Scope

Security reports are welcomed for all components of `rbit`:
- **OS-level Sandbox (`rbit/sandbox/`)**: Windows AppContainer + Job Object, Linux namespaces/Landlock/seccomp/`pivot_root`, macOS `sandbox-exec`.
- **Workspace Trust & Policy (`rbit/trust.py`, `rbit/policy.py`, `rbit/permissions.py`)**: protection against untrusted `.rbit/config.json`, hooks, MCP/LSP servers, and formatters.
- **Credential & Secret Handling (`rbit/envscrub.py`, `rbit/tools.py`)**: scrubbing LLM API keys from child processes, redacting secrets, and blocking `.env`/key reads.
- **Network & Browser (`rbit/tools.py`, `rbit/browser.py`)**: SSRF & DNS-rebinding protections, prompt-injection boundaries.
- **HTTP / Mobile Server (`rbit/cli.py` `serve`/`mobile`)**: bearer-token authentication, Origin/CSRF checks, and approval gates.

## Reporting a Vulnerability (Responsible Disclosure)

Please **do not** open a public GitHub issue for security vulnerabilities.

1. Report privately via **GitHub Security Advisories** ("Report a vulnerability") or contact the maintainer listed in `pyproject.toml`.
2. Include:
   - Affected version / commit hash and OS (`Windows 11`, `Linux`, `macOS`).
   - Minimal reproduction steps or proof-of-concept.
   - Impact assessment (sandbox escape, secret leak, RCE, auth bypass, etc.).
3. **Response SLA**:
   - Acknowledgement within **72 hours**.
   - Triage & severity assessment within **7 days**.
   - Coordinated disclosure after a fix and regression test are merged to `main`.

## Security Audits & Verification

- Sandbox audit report and re-test evidence: [`docs/security/sandbox-audit.md`](docs/security/sandbox-audit.md)
- macOS verification guide: [`docs/security/macos-verification.md`](docs/security/macos-verification.md)
- Self-test command: `rbit sandbox check`
