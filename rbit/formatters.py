"""Automatic file formatting after `write`, `edit`, `multi_edit`, and `apply_patch`.

Opt-in via config, mirroring opencode:

    {"formatter": true}
    {"formatter": {"prettier": {"disabled": true}, "deno-markdown": {"command": ["deno", "fmt", "$FILE"], "extensions": [".md"]}}}

Built-ins only run when their executable (and, for node tools, the project
dependency) is available.  A failed formatter is logged and the next matching
formatter is tried; it never fails the mutation tool itself.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import sandbox
from .envscrub import sanitized_env

logger = logging.getLogger("rbit.formatters")

FORMAT_TIMEOUT_SECONDS = 20.0

BUILTIN_FORMATTERS: dict[str, dict[str, Any]] = {
    "gofmt": {"command": ["gofmt", "-w", "$FILE"], "extensions": [".go"]},
    "mix": {"command": ["mix", "format", "$FILE"], "extensions": [".ex", ".exs", ".eex", ".heex", ".leex", ".neex", ".sface"]},
    "oxfmt": {
        "command": ["npx", "oxfmt", "--write", "$FILE"],
        "extensions": [".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts"],
        "node_dependency": "oxfmt",
    },
    "prettier": {
        "command": ["npx", "prettier", "--write", "$FILE"],
        "extensions": [
            ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts",
            ".html", ".htm", ".css", ".scss", ".sass", ".less", ".vue", ".svelte",
            ".json", ".jsonc", ".yaml", ".yml", ".toml", ".xml", ".md", ".mdx",
            ".graphql", ".gql",
        ],
        "node_dependency": "prettier",
    },
    "biome": {
        "command": ["npx", "@biomejs/biome", "format", "--write", "$FILE"],
        "extensions": [".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts", ".json", ".jsonc"],
        "node_dependency": "@biomejs/biome",
        "requires_any": ["biome.json", "biome.jsonc"],
    },
    "zig": {"command": ["zig", "fmt", "$FILE"], "extensions": [".zig", ".zon"]},
    "clang-format": {
        "command": ["clang-format", "-i", "$FILE"],
        "extensions": [".c", ".cc", ".cpp", ".cxx", ".c++", ".h", ".hh", ".hpp", ".hxx", ".h++", ".ino", ".C", ".H"],
        "requires_any": [".clang-format"],
    },
    "ktlint": {"command": ["ktlint", "-F", "$FILE"], "extensions": [".kt", ".kts"]},
    "ruff": {"command": ["ruff", "format", "$FILE"], "extensions": [".py", ".pyi"]},
    "uv": {"command": ["uv", "format", "$FILE"], "extensions": [".py", ".pyi"]},
    "rubocop": {"command": ["rubocop", "-A", "$FILE"], "extensions": [".rb", ".rake", ".gemspec", ".ru"]},
    "standardrb": {"command": ["standardrb", "--fix", "$FILE"], "extensions": [".rb", ".rake", ".gemspec", ".ru"]},
    "dart": {"command": ["dart", "format", "$FILE"], "extensions": [".dart"]},
    "ocamlformat": {"command": ["ocamlformat", "-i", "$FILE"], "extensions": [".ml", ".mli"], "requires_any": [".ocamlformat"]},
    "terraform": {"command": ["terraform", "fmt", "$FILE"], "extensions": [".tf", ".tfvars"]},
    "latexindent": {"command": ["latexindent", "-w", "$FILE"], "extensions": [".tex"]},
    "gleam": {"command": ["gleam", "format", "$FILE"], "extensions": [".gleam"]},
    "shfmt": {"command": ["shfmt", "-w", "$FILE"], "extensions": [".sh", ".bash"]},
    "nixfmt": {"command": ["nixfmt", "$FILE"], "extensions": [".nix"]},
    "rustfmt": {"command": ["rustfmt", "$FILE"], "extensions": [".rs"]},
    "pint": {"command": ["vendor/bin/pint", "$FILE"], "extensions": [".php"], "requires_any": ["composer.json"]},
    "ormolu": {"command": ["ormolu", "-i", "$FILE"], "extensions": [".hs"]},
    "cljfmt": {"command": ["cljfmt", "fix", "$FILE"], "extensions": [".clj", ".cljs", ".cljc", ".edn"]},
    "dfmt": {"command": ["dfmt", "--inplace", "$FILE"], "extensions": [".d"]},
}


@dataclass
class FormatResult:
    ran: bool = False
    ok: bool = True
    name: str = ""
    changed: bool = False
    output: str = ""


@dataclass
class FormatterDef:
    name: str
    command: list[str]
    extensions: list[str]
    environment: dict[str, str] = field(default_factory=dict)
    node_dependency: str = ""
    requires_any: list[str] = field(default_factory=list)


def formatter_enabled(config: dict[str, Any] | None) -> bool:
    if not config:
        return False
    value = config.get("formatter")
    return value is True or isinstance(value, dict)


def _project_has(workspace: Path, names: list[str]) -> bool:
    return any((workspace / name).exists() for name in names)


def _node_dependency_present(workspace: Path, package: str) -> bool:
    package_json = workspace / "package.json"
    if not package_json.exists():
        return False
    try:
        data = json.loads(package_json.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict):
        return False
    for key in ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies"):
        deps = data.get(key)
        if isinstance(deps, dict) and package in deps:
            return True
    return False


def _executable_available(workspace: Path, command: list[str], definition: FormatterDef) -> bool:
    if not command:
        return False
    first = command[0]
    if os.path.isabs(first) or os.sep in first or (os.altsep and os.altsep in first):
        candidate = Path(first)
        if not candidate.is_absolute():
            candidate = workspace / candidate
        return candidate.exists()
    if first == "npx":
        return shutil.which("npx") is not None and _node_dependency_present(workspace, definition.node_dependency)
    if definition.node_dependency and not _node_dependency_present(workspace, definition.node_dependency):
        return False
    return shutil.which(first) is not None


def _custom_definition(name: str, entry: dict[str, Any], base: FormatterDef | None) -> FormatterDef | None:
    if entry.get("disabled") is True:
        return None
    command = entry.get("command", base.command if base else None)
    extensions = entry.get("extensions", base.extensions if base else None)
    if not isinstance(command, list) or not command or not isinstance(extensions, list) or not extensions:
        return None
    environment = entry.get("environment")
    if not isinstance(environment, dict):
        environment = base.environment if base else {}
    return FormatterDef(
        name=name,
        command=[str(part) for part in command],
        extensions=[str(ext).lower() if str(ext).startswith(".") else f".{str(ext).lower()}" for ext in extensions],
        environment={str(k): str(v) for k, v in environment.items()},
        node_dependency=str(entry.get("node_dependency") or (base.node_dependency if base else "")),
        requires_any=list(entry.get("requires_any") or (base.requires_any if base else [])),
    )


def build_formatters(config: dict[str, Any] | None, workspace: Path) -> list[FormatterDef]:
    """Resolves config into an ordered list of formatter definitions."""
    if not formatter_enabled(config):
        return []
    value = (config or {}).get("formatter")
    overrides = value if isinstance(value, dict) else {}
    definitions: list[FormatterDef] = []
    for name, raw in BUILTIN_FORMATTERS.items():
        base = FormatterDef(
            name=name,
            command=[str(part) for part in raw["command"]],
            extensions=[str(ext).lower() for ext in raw["extensions"]],
            node_dependency=str(raw.get("node_dependency") or ""),
            requires_any=[str(item) for item in raw.get("requires_any") or []],
        )
        entry = overrides.get(name) if isinstance(overrides.get(name), dict) else None
        if entry is not None and entry.get("disabled") is True:
            continue
        definition = _custom_definition(name, entry or {}, base) if entry is not None else base
        if definition is None:
            continue
        if definition.requires_any and not _project_has(workspace, definition.requires_any):
            continue
        if not _executable_available(workspace, definition.command, definition):
            continue
        definitions.append(definition)
    for name, entry in overrides.items():
        if name in BUILTIN_FORMATTERS or not isinstance(entry, dict):
            continue
        definition = _custom_definition(name, entry, None)
        if definition is None:
            continue
        if definition.requires_any and not _project_has(workspace, definition.requires_any):
            continue
        if not _executable_available(workspace, definition.command, definition):
            continue
        definitions.append(definition)
    return definitions


def detect_formatter(path: Path, config: dict[str, Any] | None, workspace: Path) -> FormatterDef | None:
    suffix = path.suffix.lower()
    for definition in build_formatters(config, workspace):
        if suffix in definition.extensions:
            return definition
    return None


def formatter_is_custom(definition: FormatterDef) -> bool:
    """True when the definition runs a command the user configured (not a stock built-in).

    Such commands execute arbitrary code from the project config, so the tool
    layer asks for approval before running them."""
    base = BUILTIN_FORMATTERS.get(definition.name)
    if base is None:
        return True
    stock = [str(part) for part in base["command"]]
    return definition.command != stock or bool(definition.environment)


def format_file(
    path: Path,
    config: dict[str, Any] | None,
    workspace: Path,
    *,
    allowed: set[str] | None = None,
) -> FormatResult:
    """Runs matching formatters in order until one succeeds; failures are non-fatal.

    `allowed` restricts the run to formatters with those names (the tool layer
    passes only the ones it has cleared, e.g. custom commands after approval)."""
    suffix = path.suffix.lower()
    candidates = [
        d
        for d in build_formatters(config, workspace)
        if suffix in d.extensions and (allowed is None or d.name in allowed)
    ]
    if not candidates:
        return FormatResult(ran=False, ok=True)
    last = FormatResult(ran=False, ok=True)
    for definition in candidates:
        last = _run_formatter(path, definition, workspace, config)
        if last.ok:
            return last
    return last


def _run_formatter(path: Path, definition: FormatterDef, workspace: Path, config: dict[str, Any] | None = None) -> FormatResult:
    try:
        before = path.read_bytes()
    except OSError:
        before = b""
    command = [part.replace("$FILE", str(path.resolve())) for part in definition.command]
    if os.name == "nt" and command and command[0] == "npx":
        command[0] = shutil.which("npx") or "npx"
    env = sanitized_env()
    env.update(definition.environment)
    t0 = time.monotonic()
    try:
        if sandbox.enabled(config):
            # A project formatter is project-chosen code: workspace only, no network, no secrets.
            policy = sandbox.policy_for(config, workspace, env_values=dict(definition.environment))
            boxed = sandbox.run_sync(command, policy, timeout=FORMAT_TIMEOUT_SECONDS)
            proc = subprocess.CompletedProcess(
                command,
                boxed.returncode,
                boxed.stdout.decode("utf-8", errors="replace"),
                boxed.stderr.decode("utf-8", errors="replace"),
            )
        else:
            proc = subprocess.run(
                command,
                cwd=str(workspace),
                env=env,
                capture_output=True,
                # Formatters print UTF-8 regardless of the console code page; never let
                # a Cyrillic path or message crash the edit (cp1251 on Windows).
                encoding="utf-8",
                errors="replace",
                timeout=FORMAT_TIMEOUT_SECONDS,
                check=False,
            )
    except (OSError, subprocess.TimeoutExpired, sandbox.SandboxUnavailable) as exc:
        logger.debug("formatter %s failed for %s: %s", definition.name, path, exc)
        return FormatResult(ran=True, ok=False, name=definition.name, output=str(exc))
    output = "\n".join(part.strip() for part in (proc.stdout, proc.stderr) if part and part.strip())
    try:
        after = path.read_bytes()
    except OSError:
        after = before
    elapsed_ms = int((time.monotonic() - t0) * 1000)
    if proc.returncode != 0:
        logger.info("formatter %s exited %s for %s", definition.name, proc.returncode, path)
        return FormatResult(ran=True, ok=False, name=definition.name, changed=False, output=output)
    stdout = f"{output}\n" if output else ""
    logger.debug("formatter %s formatted %s in %sms", definition.name, path, elapsed_ms)
    return FormatResult(
        ran=True,
        ok=True,
        name=definition.name,
        changed=after != before,
        output=stdout,
    )
