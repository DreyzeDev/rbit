"""Minimal Language Server Protocol client for diagnostics (and hover).

Language servers are opt-in: set `"lsp": true` in `.rbit/config.json` (or list
specific servers).  Servers are spawned lazily, on first use, and kept alive for
the session.  Diagnostics are pulled after `didOpen`/`didChange`; server requests
that require editor capabilities are answered defensively so servers don't hang.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("rbit.lsp")

DIAGNOSTIC_TIMEOUT = 8.0
SERVER_START_TIMEOUT = 15.0
SHUTDOWN_TIMEOUT = 3.0
SEVERITY_NAMES = {1: "error", 2: "warning", 3: "info", 4: "hint"}

BUILTIN_SERVERS: dict[str, dict[str, Any]] = {
    "pyright": {
        "command": ["pyright-langserver", "--stdio"],
        "extensions": [".py", ".pyi"],
        "language_ids": {".py": "python", ".pyi": "python"},
    },
    "pylsp": {
        "command": ["pylsp"],
        "extensions": [".py", ".pyi"],
        "language_ids": {".py": "python", ".pyi": "python"},
    },
    "typescript": {
        "command": ["typescript-language-server", "--stdio"],
        "extensions": [".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts"],
        "language_ids": {
            ".ts": "typescript", ".tsx": "typescriptreact", ".js": "javascript",
            ".jsx": "javascriptreact", ".mjs": "javascript", ".cjs": "javascript",
            ".mts": "typescript", ".cts": "typescript",
        },
    },
    "gopls": {"command": ["gopls", "serve"], "extensions": [".go"], "language_ids": {".go": "go"}},
    "rust-analyzer": {"command": ["rust-analyzer"], "extensions": [".rs"], "language_ids": {".rs": "rust"}},
    "clangd": {
        "command": ["clangd"],
        "extensions": [".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".hh", ".hxx"],
        "language_ids": {".c": "c", ".h": "c", ".cc": "cpp", ".cpp": "cpp", ".cxx": "cpp", ".hpp": "cpp"},
    },
    "bash": {
        "command": ["bash-language-server", "start"],
        "extensions": [".sh", ".bash"],
        "language_ids": {".sh": "shellscript", ".bash": "shellscript"},
    },
    "yaml": {
        "command": ["yaml-language-server", "--stdio"],
        "extensions": [".yaml", ".yml"],
        "language_ids": {".yaml": "yaml", ".yml": "yaml"},
    },
    "json": {
        "command": ["vscode-json-language-server", "--stdio"],
        "extensions": [".json", ".jsonc"],
        "language_ids": {".json": "json", ".jsonc": "jsonc"},
    },
    "css": {
        "command": ["vscode-css-language-server", "--stdio"],
        "extensions": [".css", ".scss", ".less"],
        "language_ids": {".css": "css", ".scss": "scss", ".less": "less"},
    },
    "html": {
        "command": ["vscode-html-language-server", "--stdio"],
        "extensions": [".html", ".htm"],
        "language_ids": {".html": "html", ".htm": "html"},
    },
    "zls": {"command": ["zls"], "extensions": [".zig"], "language_ids": {".zig": "zig"}},
    "terraform": {"command": ["terraform-ls", "serve"], "extensions": [".tf", ".tfvars"], "language_ids": {".tf": "terraform"}},
    "lua": {"command": ["lua-language-server"], "extensions": [".lua"], "language_ids": {".lua": "lua"}},
    "php": {"command": ["intelephense", "--stdio"], "extensions": [".php"], "language_ids": {".php": "php"}},
    "ruby": {"command": ["solargraph", "stdio"], "extensions": [".rb"], "language_ids": {".rb": "ruby"}},
    "swift": {"command": ["sourcekit-lsp"], "extensions": [".swift"], "language_ids": {".swift": "swift"}},
}


@dataclass
class LSPServerDef:
    name: str
    command: list[str]
    extensions: list[str]
    language_ids: dict[str, str] = field(default_factory=dict)
    initialization: dict[str, Any] = field(default_factory=dict)


@dataclass
class LSPDiagnostic:
    path: Path
    line: int
    column: int
    severity: str
    message: str
    source: str = ""


def lsp_enabled(config: dict[str, Any] | None) -> bool:
    if not config:
        return False
    value = config.get("lsp")
    return value is True or isinstance(value, dict)


def _local_bin(workspace: Path, name: str) -> str | None:
    suffix = ".cmd" if os.name == "nt" else ""
    candidate = workspace / "node_modules" / ".bin" / f"{name}{suffix}"
    if candidate.exists():
        return str(candidate)
    return shutil.which(name)


def _resolve_command(workspace: Path, command: list[str]) -> list[str] | None:
    if not command:
        return None
    first = command[0]
    if os.path.isabs(first) or os.sep in first:
        resolved = command[0] if Path(command[0]).exists() else None
        if resolved is None:
            return None
        return _wrap_batch(resolved, command[1:])
    resolved = _local_bin(workspace, first)
    if resolved is None:
        return None
    return _wrap_batch(resolved, command[1:])


def _wrap_batch(executable: str, args: list[str]) -> list[str]:
    if os.name == "nt" and executable.lower().endswith((".cmd", ".bat")):
        return [os.environ.get("COMSPEC", "cmd.exe"), "/c", executable, *args]
    return [executable, *args]


def build_servers(config: dict[str, Any] | None, workspace: Path) -> list[LSPServerDef]:
    if not lsp_enabled(config):
        return []
    value = (config or {}).get("lsp")
    overrides = value if isinstance(value, dict) else {}
    definitions: list[LSPServerDef] = []
    for name, raw in BUILTIN_SERVERS.items():
        entry = overrides.get(name)
        if isinstance(entry, dict):
            if entry.get("disabled") is True:
                continue
            command = [str(part) for part in entry.get("command", raw["command"])]
            extensions = [str(ext).lower() for ext in entry.get("extensions", raw["extensions"])]
            initialization = entry.get("initialization")
            if not isinstance(initialization, dict):
                initialization = {}
        else:
            command = [str(part) for part in raw["command"]]
            extensions = [str(ext).lower() for ext in raw["extensions"]]
            initialization = {}
        resolved = _resolve_command(workspace, command)
        if resolved is None:
            continue
        definitions.append(
            LSPServerDef(
                name=name,
                command=resolved,
                extensions=extensions,
                language_ids={str(k).lower(): str(v) for k, v in raw.get("language_ids", {}).items()},
                initialization=initialization,
            )
        )
    for name, entry in overrides.items():
        if name in BUILTIN_SERVERS or not isinstance(entry, dict) or entry.get("disabled") is True:
            continue
        command = entry.get("command")
        extensions = entry.get("extensions")
        if not isinstance(command, list) or not command or not isinstance(extensions, list) or not extensions:
            continue
        resolved = _resolve_command(workspace, [str(part) for part in command])
        if resolved is None:
            continue
        initialization = entry.get("initialization")
        definitions.append(
            LSPServerDef(
                name=name,
                command=resolved,
                extensions=[str(ext).lower() if str(ext).startswith(".") else f".{str(ext).lower()}" for ext in extensions],
                language_ids={str(k).lower(): str(v) for k, v in (entry.get("language_ids") or {}).items()},
                initialization=initialization if isinstance(initialization, dict) else {},
            )
        )
    return definitions


def utf16_units(text: str, index: int) -> int:
    """UTF-16 code units in text[:index] (LSP positions are UTF-16 based)."""
    return len(text[: max(0, index)].encode("utf-16-le", "surrogatepass")) // 2


def utf16_to_index(text: str, units: int) -> int:
    """Convert a UTF-16 column to a Python str index."""
    count = 0
    for idx, ch in enumerate(text):
        if count >= units:
            return idx
        count += 2 if ord(ch) > 0xFFFF else 1
    return len(text)


def _read_lines(path: Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []


def _server_column(path: Path, line0: int, units: int) -> int:
    """1-based character column for a 0-based line and UTF-16 column from a server."""
    lines = _read_lines(path)
    if 0 <= line0 < len(lines):
        return utf16_to_index(lines[line0], units) + 1
    return units + 1


def _client_position(path: Path, line: int, column: int) -> dict[str, int]:
    """LSP position from 1-based line / character column."""
    line0 = max(0, line - 1)
    col0 = max(0, column - 1)
    lines = _read_lines(path)
    if line0 < len(lines):
        col0 = utf16_units(lines[line0], col0)
    return {"line": line0, "character": col0}


def _coerce_id(value: Any) -> Any:
    """JSON-RPC ids may be ints or strings; normalise numeric strings."""
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return value
    return value


def _encode_message(payload: dict[str, Any]) -> bytes:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body


class LSPClient:
    def __init__(self, definition: LSPServerDef, workspace: Path) -> None:
        self.definition = definition
        self.workspace = workspace.resolve()
        self.process: asyncio.subprocess.Process | None = None
        self._next_id = 0
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._diagnostics: dict[str, list[LSPDiagnostic]] = {}
        self._diagnostics_event = asyncio.Event()
        self._opened: dict[str, int] = {}
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._failed: str = ""

    @property
    def failed(self) -> str:
        return self._failed

    async def start(self) -> bool:
        if self.process is not None:
            return True
        if self._failed:
            # Negative cache: do not respawn a server that already failed.
            return False
        try:
            self.process = await asyncio.create_subprocess_exec(
                *self.definition.command,
                cwd=str(self.workspace),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            self._failed = str(exc)
            logger.debug("lsp %s failed to start: %s", self.definition.name, exc)
            return False
        self._reader_task = asyncio.create_task(self._read_loop())
        self._stderr_task = asyncio.create_task(self._drain_stderr())
        try:
            result = await asyncio.wait_for(self._request("initialize", self._initialize_params()), SERVER_START_TIMEOUT)
        except (asyncio.TimeoutError, RuntimeError, OSError) as exc:
            self._failed = f"initialize failed: {exc}"
            logger.debug("lsp %s %s", self.definition.name, self._failed)
            await self.close()
            return False
        if isinstance(result, dict):
            logger.debug("lsp %s initialized: %s", self.definition.name, result.get("serverInfo"))
        await self._notify("initialized", {})
        return True

    def _initialize_params(self) -> dict[str, Any]:
        root_uri = self.workspace.as_uri()
        params: dict[str, Any] = {
            "processId": os.getpid(),
            "clientInfo": {"name": "rbit", "version": "1.3.1"},
            "rootUri": root_uri,
            "rootPath": str(self.workspace),
            "workspaceFolders": [{"uri": root_uri, "name": self.workspace.name}],
            "capabilities": {
                "workspace": {"workspaceFolders": True, "configuration": True},
                "textDocument": {
                    "synchronization": {"dynamicRegistration": False, "didSave": True},
                    "publishDiagnostics": {"relatedInformation": True, "versionSupport": True},
                    "hover": {"contentFormat": ["markdown", "plaintext"]},
                },
            },
            "initializationOptions": self.definition.initialization,
        }
        return params

    async def _drain_stderr(self) -> None:
        assert self.process is not None and self.process.stderr is not None
        try:
            while True:
                line = await self.process.stderr.readline()
                if not line:
                    break
                logger.debug("lsp %s stderr: %s", self.definition.name, line.decode("utf-8", "replace").rstrip())
        except (asyncio.CancelledError, OSError):
            return

    async def _read_loop(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        stdout = self.process.stdout
        try:
            while True:
                header = await stdout.readline()
                if not header:
                    break
                if not header.lower().startswith(b"content-length:"):
                    continue
                try:
                    length = int(header.split(b":", 1)[1].strip())
                except ValueError:
                    continue
                while True:
                    blank = await stdout.readline()
                    if blank in (b"\r\n", b"\n", b""):
                        break
                body = await stdout.readexactly(length)
                try:
                    message = json.loads(body.decode("utf-8"))
                    if isinstance(message, dict):
                        await self._handle_message(message)
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 - one bad message must not kill the reader
                    logger.debug("lsp %s: bad message ignored", self.definition.name, exc_info=True)
        except (asyncio.CancelledError, asyncio.IncompleteReadError, OSError, ValueError):
            return
        finally:
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(RuntimeError("language server exited"))
            self._pending.clear()

    async def _handle_message(self, message: dict[str, Any]) -> None:
        if "id" in message and ("result" in message or "error" in message):
            future = self._pending.pop(_coerce_id(message["id"]), None)
            if future is not None and not future.done():
                if "error" in message:
                    future.set_exception(RuntimeError(str(message["error"])))
                else:
                    future.set_result(message.get("result"))
            return
        if "id" in message and "method" in message:
            await self._handle_server_request(message["id"], str(message["method"]), message.get("params"))
            return
        method = message.get("method")
        params = message.get("params") or {}
        if method == "textDocument/publishDiagnostics" and isinstance(params, dict):
            uri = str(params.get("uri") or "")
            self._diagnostics[uri] = self._parse_diagnostics(uri, params.get("diagnostics") or [])
            self._diagnostics_event.set()
        elif method in ("window/logMessage", "window/showMessage"):
            logger.debug("lsp %s %s: %s", self.definition.name, method, params.get("message"))

    async def _handle_server_request(self, request_id: Any, method: str, params: Any) -> None:
        if method == "workspace/configuration":
            items = params.get("items") if isinstance(params, dict) else None
            result: Any = [None for _ in items] if isinstance(items, list) else []
        elif method in ("workspace/workspaceFolders",):
            result = [{"uri": self.workspace.as_uri(), "name": self.workspace.name}]
        elif method in ("client/registerCapability", "client/unregisterCapability", "window/workDoneProgress/create"):
            result = None
        else:
            await self._send({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "unsupported"}})
            return
        await self._send({"jsonrpc": "2.0", "id": request_id, "result": result})

    def _parse_diagnostics(self, uri: str, items: list[Any]) -> list[LSPDiagnostic]:
        path = self._uri_to_path(uri)
        parsed: list[LSPDiagnostic] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            start = item.get("range", {}).get("start", {})
            line0 = int(start.get("line", 0))
            parsed.append(
                LSPDiagnostic(
                    path=path,
                    line=line0 + 1,
                    column=_server_column(path, line0, int(start.get("character", 0))),
                    severity=SEVERITY_NAMES.get(int(item.get("severity", 1)), "error"),
                    message=str(item.get("message") or "").strip(),
                    source=str(item.get("source") or self.definition.name),
                )
            )
        return parsed

    def _uri_to_path(self, uri: str) -> Path:
        if uri.startswith("file:"):
            parsed = urlparse(uri)
            raw = url2pathname(parsed.path)
            if parsed.netloc and parsed.netloc != "localhost":
                raw = f"//{unquote(parsed.netloc)}{unquote(parsed.path)}" if os.name == "nt" else raw
            return Path(raw).resolve()
        return Path(unquote(uri))

    async def _send(self, payload: dict[str, Any]) -> None:
        if self.process is None or self.process.stdin is None:
            raise RuntimeError("language server is not running")
        self.process.stdin.write(_encode_message(payload))
        await self.process.stdin.drain()

    async def _request(self, method: str, params: dict[str, Any]) -> Any:
        self._next_id += 1
        request_id = self._next_id
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        await self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        return await future

    async def _notify(self, method: str, params: dict[str, Any]) -> None:
        await self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def language_id(self, path: Path) -> str:
        return self.definition.language_ids.get(path.suffix.lower(), "plaintext")

    async def open_file(self, path: Path) -> bool:
        if self.process is None and not await self.start():
            return False
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return False
        uri = path.resolve().as_uri()
        if uri in self._opened:
            version = self._opened[uri] + 1
            self._opened[uri] = version
            self._diagnostics_event.clear()
            try:
                await self._notify(
                    "textDocument/didChange",
                    {"textDocument": {"uri": uri, "version": version}, "contentChanges": [{"text": text}]},
                )
            except RuntimeError:
                return False
        else:
            version = 1
            self._opened[uri] = version
            self._diagnostics_event.clear()
            try:
                await self._notify(
                    "textDocument/didOpen",
                    {
                        "textDocument": {
                            "uri": uri,
                            "languageId": self.language_id(path),
                            "version": version,
                            "text": text,
                        }
                    },
                )
            except RuntimeError:
                return False
        try:
            await asyncio.wait_for(self._diagnostics_event.wait(), DIAGNOSTIC_TIMEOUT)
        except asyncio.TimeoutError:
            logger.debug("lsp %s: no diagnostics for %s within timeout", self.definition.name, path)
        return True

    def diagnostics(self, path: Path) -> list[LSPDiagnostic]:
        return self._diagnostics.get(path.resolve().as_uri(), [])

    async def document_symbols(self, path: Path) -> list[dict[str, Any]]:
        if self.process is None and not await self.start():
            return []
        uri = path.resolve().as_uri()
        try:
            result = await asyncio.wait_for(
                self._request("textDocument/documentSymbol", {"textDocument": {"uri": uri}}), 8.0
            )
        except (asyncio.TimeoutError, RuntimeError, OSError):
            return []
        items = result if isinstance(result, list) else []
        symbols: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            range_info = item.get("range") or item.get("location", {}).get("range") or {}
            start = range_info.get("start", {}) if isinstance(range_info, dict) else {}
            line0 = int(start.get("line", 0))
            symbols.append(
                {
                    "name": str(item.get("name") or ""),
                    "kind": int(item.get("kind") or 0),
                    "line": line0 + 1,
                    "column": _server_column(path, line0, int(start.get("character", 0))),
                }
            )
        return symbols

    async def rename(
        self, path: Path, line: int, column: int, new_name: str
    ) -> list[tuple[Path, dict[str, int], str]]:
        if self.process is None and not await self.start():
            return []
        uri = path.resolve().as_uri()
        params = {
            "textDocument": {"uri": uri},
            "position": _client_position(path, line, column),
            "newName": new_name,
        }
        try:
            result = await asyncio.wait_for(self._request("textDocument/rename", params), 10.0)
        except (asyncio.TimeoutError, RuntimeError, OSError):
            return []
        return self._parse_workspace_edit(result if isinstance(result, dict) else {})

    def _parse_workspace_edit(self, edit: dict[str, Any]) -> list[tuple[Path, dict[str, int], str]]:
        edits: list[tuple[Path, dict[str, int], str]] = []
        changes = edit.get("changes")
        if isinstance(changes, dict):
            for uri, text_edits in changes.items():
                target = self._uri_to_path(str(uri))
                if not self._inside_workspace(target):
                    continue
                for item in text_edits if isinstance(text_edits, list) else []:
                    if not isinstance(item, dict):
                        continue
                    range_info = item.get("range") or {}
                    start = range_info.get("start", {}) if isinstance(range_info, dict) else {}
                    end = range_info.get("end", {}) if isinstance(range_info, dict) else {}
                    edits.append(
                        (
                            target,
                            {
                                "start_line": int(start.get("line", 0)),
                                "start_column": int(start.get("character", 0)),
                                "end_line": int(end.get("line", 0)),
                                "end_column": int(end.get("character", 0)),
                            },
                            str(item.get("newText") or ""),
                        )
                    )
        document_changes = edit.get("documentChanges")
        if isinstance(document_changes, list):
            for change in document_changes:
                if not isinstance(change, dict):
                    continue
                text_document = change.get("textDocument")
                uri = text_document.get("uri") if isinstance(text_document, dict) else ""
                target = self._uri_to_path(str(uri)) if uri else None
                if target is None or not self._inside_workspace(target):
                    continue
                for item in change.get("edits") or []:
                    if not isinstance(item, dict):
                        continue
                    range_info = item.get("range") or {}
                    start = range_info.get("start", {}) if isinstance(range_info, dict) else {}
                    end = range_info.get("end", {}) if isinstance(range_info, dict) else {}
                    edits.append(
                        (
                            target,
                            {
                                "start_line": int(start.get("line", 0)),
                                "start_column": int(start.get("character", 0)),
                                "end_line": int(end.get("line", 0)),
                                "end_column": int(end.get("character", 0)),
                            },
                            str(item.get("newText") or ""),
                        )
                    )
        return edits

    def _inside_workspace(self, path: Path) -> bool:
        try:
            path.resolve().relative_to(self.workspace)
        except (ValueError, OSError):
            return False
        return True

    def apply_edits(self, edits: list[tuple[Path, dict[str, int], str]]) -> int:
        by_file: dict[Path, list[tuple[dict[str, int], str]]] = {}
        for path, range_info, new_text in edits:
            by_file.setdefault(path, []).append((range_info, new_text))
        applied = 0
        for path, file_edits in by_file.items():
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
            except OSError:
                continue
            for range_info, new_text in sorted(
                file_edits,
                key=lambda item: (
                    -item[0]["start_line"],
                    -item[0]["start_column"],
                    -item[0]["end_line"],
                    -item[0]["end_column"],
                ),
            ):
                try:
                    start_line = int(range_info["start_line"])
                    end_line = int(range_info["end_line"])
                except (KeyError, TypeError, ValueError):
                    continue
                if start_line < 0 or end_line >= len(lines):
                    continue
                start_text = lines[start_line]
                start_column = utf16_to_index(start_text, int(range_info.get("start_column", 0)))
                end_text = lines[end_line]
                end_column = utf16_to_index(end_text, int(range_info.get("end_column", 0)))
                prefix = start_text[:start_column]
                suffix = end_text[end_column:]
                lines[start_line : end_line + 1] = [prefix + new_text + suffix]
                applied += 1
            try:
                path.write_text("".join(lines), encoding="utf-8")
            except OSError:
                continue
        return applied

    async def hover(self, path: Path, line: int, column: int) -> str:
        if self.process is None and not await self.start():
            return ""
        uri = path.resolve().as_uri()
        try:
            result = await asyncio.wait_for(
                self._request(
                    "textDocument/hover",
                    {"textDocument": {"uri": uri}, "position": _client_position(path, line, column)},
                ),
                5.0,
            )
        except (asyncio.TimeoutError, RuntimeError, OSError):
            return ""
        if not isinstance(result, dict):
            return ""
        contents = result.get("contents")
        if isinstance(contents, dict):
            return str(contents.get("value") or "")
        if isinstance(contents, list):
            return "\n".join(str(item.get("value") if isinstance(item, dict) else item) for item in contents)
        return str(contents or "")

    async def locations(self, method: str, path: Path, line: int, column: int) -> list[dict[str, Any]]:
        if self.process is None and not await self.start():
            return []
        uri = path.resolve().as_uri()
        params = {
            "textDocument": {"uri": uri},
            "position": _client_position(path, line, column),
        }
        if method == "textDocument/references":
            params["context"] = {"includeDeclaration": True}
        try:
            result = await asyncio.wait_for(self._request(method, params), 8.0)
        except (asyncio.TimeoutError, RuntimeError, OSError):
            return []
        items = result if isinstance(result, list) else [result] if isinstance(result, dict) else []
        locations = []
        for item in items:
            if not isinstance(item, dict):
                continue
            target = item.get("targetUri") if "targetUri" in item else item.get("uri")
            range_info = item.get("targetSelectionRange") if "targetSelectionRange" in item else item.get("range")
            if not target or not isinstance(range_info, dict):
                continue
            position = range_info.get("start") or {}
            target_path = self._uri_to_path(str(target))
            line0 = int(position.get("line", 0))
            locations.append(
                {
                    "path": str(target_path),
                    "line": line0 + 1,
                    "column": _server_column(target_path, line0, int(position.get("character", 0))),
                }
            )
        return locations

    async def close(self) -> None:
        if self.process is None:
            return
        try:
            if self.process.returncode is None:
                try:
                    await asyncio.wait_for(self._request("shutdown", {}), SHUTDOWN_TIMEOUT)
                except (asyncio.TimeoutError, RuntimeError, OSError):
                    pass
                try:
                    await asyncio.wait_for(self._notify("exit", {}), 1.0)
                except (asyncio.TimeoutError, RuntimeError, OSError):
                    pass
                if self.process.returncode is None:
                    self.process.terminate()
        except ProcessLookupError:
            pass
        for task in (self._reader_task, self._stderr_task):
            if task is not None:
                task.cancel()
        try:
            await asyncio.wait_for(self.process.wait(), SHUTDOWN_TIMEOUT)
        except (asyncio.TimeoutError, ProcessLookupError):
            try:
                self.process.kill()
            except ProcessLookupError:
                pass
        self.process = None


class LSPManager:
    def __init__(self, workspace: Path, servers: list[LSPServerDef]) -> None:
        self.workspace = workspace.resolve()
        self.servers = servers
        self._clients: dict[str, LSPClient] = {}
        self._by_extension: dict[str, LSPServerDef] = {}
        for server in servers:
            for extension in server.extensions:
                self._by_extension.setdefault(extension.lower(), server)

    @classmethod
    def from_config(cls, workspace: Path, config: dict[str, Any] | None) -> LSPManager:
        return cls(workspace, build_servers(config, workspace))

    @property
    def configured_count(self) -> int:
        return len(self.servers)

    def definition_for(self, path: Path) -> LSPServerDef | None:
        return self._by_extension.get(path.suffix.lower())

    def client_for(self, path: Path) -> LSPClient | None:
        definition = self.definition_for(path)
        if definition is None:
            return None
        client = self._clients.get(definition.name)
        if client is None:
            client = LSPClient(definition, self.workspace)
            self._clients[definition.name] = client
        return client

    async def diagnostics_for(self, path: Path) -> list[LSPDiagnostic]:
        client = self.client_for(path)
        if client is None:
            return []
        if not await client.open_file(path):
            return []
        return client.diagnostics(path)

    async def diagnostics_for_many(self, paths: list[Path]) -> dict[Path, list[LSPDiagnostic]]:
        results: dict[Path, list[LSPDiagnostic]] = {}
        for path in paths:
            results[path] = await self.diagnostics_for(path)
        return results

    async def hover(self, path: Path, line: int, column: int) -> str:
        client = self.client_for(path)
        if client is None:
            return ""
        return await client.hover(path, line, column)

    async def definition(self, path: Path, line: int, column: int) -> list[dict[str, Any]]:
        client = self.client_for(path)
        if client is None:
            return []
        return await client.locations("textDocument/definition", path, line, column)

    async def references(self, path: Path, line: int, column: int) -> list[dict[str, Any]]:
        client = self.client_for(path)
        if client is None:
            return []
        return await client.locations("textDocument/references", path, line, column)

    async def symbols(self, path: Path) -> list[dict[str, Any]]:
        client = self.client_for(path)
        if client is None:
            return []
        return await client.document_symbols(path)

    async def rename(self, path: Path, line: int, column: int, new_name: str) -> tuple[int, list[Path]]:
        client = self.client_for(path)
        if client is None:
            return 0, []
        edits = await client.rename(path, line, column, new_name)
        applied = client.apply_edits(edits)
        return applied, sorted({edit_path for edit_path, _range, _text in edits})

    def status_lines(self) -> list[str]:
        lines = []
        for definition in self.servers:
            client = self._clients.get(definition.name)
            if client is None:
                state = "available"
            elif client.failed:
                state = f"failed: {client.failed}"
            elif client.process is not None and client.process.returncode is None:
                state = "running"
            else:
                state = "stopped"
            lines.append(f"{definition.name}: {state}")
        return lines

    async def close(self) -> None:
        clients = list(self._clients.values())
        self._clients.clear()
        for client in clients:
            await client.close()

    def terminate(self) -> None:
        for client in self._clients.values():
            process = client.process
            if process is not None and process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
        self._clients.clear()
