"""Model Context Protocol (MCP) client for rbit.

Supports local stdio servers (``type: "local"``) and remote streamable-HTTP
servers (``type: "remote"``).  Servers are configured either under the
opencode-compatible ``mcp`` key or the legacy ``mcp_servers`` mapping::

    {
      "mcp": {
        "fs": {"type": "local", "command": ["npx", "-y", "@modelcontextprotocol/server-filesystem", "."]},
        "docs": {"type": "remote", "url": "https://example.com/mcp", "headers": {"Authorization": "Bearer ..."}}
      }
    }

Every discovered tool is exposed to the model as ``<server>_<tool>``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shlex
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from . import __version__, sandbox

logger = logging.getLogger("rbit.mcp")

PROTOCOL_VERSION = "2024-11-05"
MAX_TOOL_OUTPUT = 80_000
MAX_STDERR_TAIL = 40
STREAM_LIMIT = 64 * 1024 * 1024  # a single JSON-RPC line may be large (tool results)

# Environment variables safe to hand to MCP servers.  Secrets such as
# RBIT_* / *_API_KEY / tokens are NOT inherited; pass them via `environment`.
_ENV_ALLOWLIST = {
    "PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "HOME", "USERPROFILE",
    "HOMEDRIVE", "HOMEPATH", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "PROGRAMFILES",
    "PROGRAMFILES(X86)", "TEMP", "TMP", "TMPDIR", "USER", "USERNAME", "LOGNAME", "SHELL", "LANG",
    "LC_ALL", "LC_CTYPE", "TERM", "TZ", "NODE_PATH", "NODE_OPTIONS", "NPM_CONFIG_PREFIX",
    "VIRTUAL_ENV", "PYTHONPATH", "PYTHONIOENCODING", "PYTHONUTF8", "XDG_CONFIG_HOME",
    "XDG_DATA_HOME", "XDG_CACHE_HOME", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "SSL_CERT_FILE",
}


def _base_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k.upper() in _ENV_ALLOWLIST}


class MCPError(RuntimeError):
    """A JSON-RPC / transport failure from an MCP server."""


@dataclass
class MCPTool:
    server: str
    raw_name: str
    exposed_name: str
    description: str
    input_schema: dict[str, Any]

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.exposed_name,
                "description": f"[{self.server}] {self.description}".strip(),
                "parameters": self.input_schema or {"type": "object", "properties": {}},
            },
        }


@dataclass
class MCPCallResult:
    ok: bool
    summary: str
    output: str


@dataclass
class MCPClient:
    name: str
    config: dict[str, Any]
    workspace: Path
    http_client: httpx.AsyncClient | None = None
    tools: list[MCPTool] = field(default_factory=list)
    server_info: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    transport: str = "stdio"
    #: The whole rbit configuration: decides whether the server is started inside the sandbox.
    global_config: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.workspace = Path(self.workspace).resolve()
        self.command = self._command_argv()
        self.url = str(self.config.get("url") or "").strip()
        self.headers = {str(k): str(v) for k, v in (self.config.get("headers") or {}).items()}
        self.timeout = float(self.config.get("timeout") or 30)
        self.call_timeout = float(self.config.get("call_timeout") or 120)
        self.enabled = self.config.get("enabled", True) is not False
        if self.command:
            self.transport = "stdio"
        elif self.url:
            self.transport = "http"
        else:
            self.transport = "invalid"
        self._proc: asyncio.subprocess.Process | None = None
        self._reader: asyncio.Task[None] | None = None
        self._stderr: asyncio.Task[None] | None = None
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._next_id = 0
        self._stderr_tail: list[str] = []
        self._session_id = ""
        self._closed = False
        self._owned_http = False
        self._started = False

    # --- configuration ---
    def _command_argv(self) -> list[str]:
        raw = self.config.get("command")
        if isinstance(raw, str):
            raw = shlex.split(raw, posix=(os.name != "nt"))
        if not isinstance(raw, (list, tuple)):
            return []
        argv = [str(part) for part in raw if str(part)]
        if argv:
            resolved = _which(argv[0])
            if resolved:
                argv[0] = resolved
        return argv

    @property
    def connected(self) -> bool:
        if self.transport == "stdio":
            return self._proc is not None and self._proc.returncode is None and not self.error
        if self.transport == "http":
            return self._started and not self.error
        return False

    # --- lifecycle ---
    async def start(self) -> None:
        try:
            if not self.enabled:
                self.error = "disabled"
                return
            if self.transport == "invalid":
                raise MCPError("configure either `command` (local) or `url` (remote)")
            if self.transport == "stdio":
                await self._start_stdio()
            else:
                await self._start_http()

            init = await self._request(
                "initialize",
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "rbit", "version": __version__},
                },
                timeout=self.timeout,
            )
            self.server_info = init.get("serverInfo") or {}
            await self._notify("notifications/initialized", {})
            self.tools = await self._list_tools()
            self._started = True
        except Exception as exc:  # noqa: BLE001 - any transport failure is reported, not raised
            self.error = self._format_error(exc)
            logger.info("MCP server %s failed: %s", self.name, self.error)
            await self.close()

    def _format_error(self, exc: BaseException) -> str:
        text = str(exc).strip() or type(exc).__name__
        tail = self.stderr_tail()
        if tail and isinstance(exc, (MCPError, asyncio.TimeoutError, OSError)):
            text = f"{text} ({tail.splitlines()[-1][:160]})"
        return text[:500]

    def stderr_tail(self) -> str:
        return "\n".join(self._stderr_tail[-MAX_STDERR_TAIL:])

    async def _start_stdio(self) -> None:
        env = _base_env()
        extra_env = self.config.get("environment") or self.config.get("env") or {}
        if isinstance(extra_env, dict):
            env.update({str(k): str(v) for k, v in extra_env.items()})
        cwd = self.config.get("cwd") or str(self.workspace)
        sandbox.ensure(self.global_config)
        if sandbox.enabled(self.global_config):
            # An MCP server is third-party code (often `npx -y ...`): it gets the workspace, no
            # network and no secrets unless its own "sandbox" block (or "environment") says so.
            # The sandbox's workspace is the PROJECT workspace, never the server's `cwd`: a `cwd`
            # elsewhere (`~/.ssh`, `~`) must not turn that folder into a writable workspace. It
            # may only choose a folder inside the workspace to start in.
            policy = sandbox.policy_for(
                self.global_config,
                self.workspace,
                self.config.get("sandbox"),
                env_values={str(k): str(v) for k, v in extra_env.items()} if isinstance(extra_env, dict) else None,
                cwd=Path(cwd),
            )
            self._proc = await sandbox.spawn(list(self.command), policy, stdin=True, stderr="pipe")
        else:
            self._proc = await asyncio.create_subprocess_exec(
                *self.command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(cwd),
                env=env,
                limit=STREAM_LIMIT,
            )
        self._reader = asyncio.create_task(self._read_loop())
        self._stderr = asyncio.create_task(self._stderr_loop())

    async def _start_http(self) -> None:
        if self.http_client is None:
            self.http_client = httpx.AsyncClient(timeout=httpx.Timeout(self.call_timeout))
            self._owned_http = True

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for fut in self._pending.values():
            if not fut.done():
                fut.cancel()
        self._pending.clear()
        if self._reader is not None:
            self._reader.cancel()
            self._reader = None
        if self._stderr is not None:
            self._stderr.cancel()
            self._stderr = None
        proc = self._proc
        self._proc = None
        if proc is not None and proc.returncode is None:
            if proc.stdin is not None:
                try:
                    proc.stdin.close()
                except Exception:  # noqa: BLE001 - already closed
                    pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                try:
                    proc.terminate()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(proc.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=2.0)
                    except asyncio.TimeoutError:
                        logger.debug("MCP %s: process did not exit after kill", self.name)
        if self._owned_http and self.http_client is not None:
            try:
                await self.http_client.aclose()
            except Exception as exc:  # noqa: BLE001 - shutdown must not raise
                logger.debug("MCP %s http close failed: %s", self.name, exc)
            self.http_client = None

    def terminate(self) -> None:
        """Best-effort synchronous cleanup for app shutdown (no await possible)."""
        self._closed = True
        proc = self._proc
        self._proc = None
        if proc is not None and proc.returncode is None:
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
        for task in (self._reader, self._stderr):
            if task is not None:
                task.cancel()
        if self.http_client is not None and self._owned_http:
            try:
                asyncio.get_running_loop().create_task(self.http_client.aclose())
            except RuntimeError:
                pass

    # --- JSON-RPC ---
    def _next_request_id(self) -> int:
        self._next_id += 1
        return self._next_id

    async def _request(self, method: str, params: dict[str, Any], timeout: float | None = None) -> dict[str, Any]:
        if self._closed:
            raise MCPError("client is closed")
        rid = self._next_request_id()
        payload = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}
        if self.transport == "stdio":
            result = await self._stdio_request(rid, payload, timeout or self.call_timeout)
        else:
            result = await self._http_request(payload, timeout or self.call_timeout)
        if "error" in result:
            err = result.get("error") or {}
            raise MCPError(str(err.get("message") or err))
        return result.get("result") or {}

    async def _notify(self, method: str, params: dict[str, Any]) -> None:
        if self._closed:
            return
        payload = {"jsonrpc": "2.0", "method": method, "params": params}
        if self.transport == "stdio":
            proc = self._proc
            if proc is None or proc.stdin is None:
                return
            proc.stdin.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
            try:
                await proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass
        else:
            await self._http_post(payload, expect_response=False)

    async def _stdio_request(self, rid: int, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise MCPError("server process is not running")
        if self._reader is None or self._reader.done() or self.error:
            raise MCPError(self.error or "server connection is closed")
        fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        try:
            proc.stdin.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
            await asyncio.wait_for(proc.stdin.drain(), timeout=timeout)
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            raise MCPError(f"timed out after {timeout:.0f}s waiting for {payload['method']}") from None
        finally:
            self._pending.pop(rid, None)

    async def _http_request(self, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
        response = await self._http_post(payload, expect_response=True, timeout=timeout)
        return response

    async def _http_post(
        self, payload: dict[str, Any], *, expect_response: bool, timeout: float | None = None
    ) -> dict[str, Any]:
        client = self.http_client
        if client is None:
            raise MCPError("HTTP transport is not initialized")
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            **self.headers,
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        try:
            resp = await client.post(self.url, json=payload, headers=headers, timeout=timeout or self.call_timeout)
        except httpx.HTTPError as exc:
            raise MCPError(f"HTTP error: {exc}") from exc
        if resp.status_code == 401:
            raise MCPError("HTTP 401: server requires authentication (set `headers.Authorization`)")
        if resp.status_code >= 400:
            raise MCPError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        sid = resp.headers.get("mcp-session-id")
        if sid:
            self._session_id = sid
        if not expect_response:
            return {}
        content_type = resp.headers.get("content-type", "")
        if "text/event-stream" in content_type:
            return self._parse_sse(resp.text, payload.get("id"))
        try:
            data = resp.json()
        except ValueError:
            raise MCPError(f"invalid JSON response: {resp.text[:200]}") from None
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and item.get("id") == payload.get("id"):
                    return item
            raise MCPError("response did not contain the request id")
        if not isinstance(data, dict):
            raise MCPError("invalid JSON-RPC response")
        return data

    @staticmethod
    def _parse_sse(text: str, request_id: Any) -> dict[str, Any]:
        messages: list[dict[str, Any]] = []
        data_lines: list[str] = []

        def flush() -> None:
            if not data_lines:
                return
            raw = "\n".join(data_lines).strip()
            data_lines.clear()
            if not raw or raw == "[DONE]":
                return
            try:
                parsed = json.loads(raw)
            except ValueError:
                return
            if isinstance(parsed, dict):
                messages.append(parsed)

        for line in text.splitlines():
            if not line.strip():
                flush()
            elif line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
        flush()
        if not messages:
            raise MCPError("empty SSE response")
        for msg in messages:
            if msg.get("id") == request_id:
                return msg
        return messages[-1]

    async def _read_loop(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        try:
            while True:
                try:
                    line = await proc.stdout.readline()
                except (ValueError, asyncio.LimitOverrunError) as exc:
                    self.error = f"server sent a line longer than the {STREAM_LIMIT // (1024 * 1024)} MiB limit: {exc}"
                    break
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").strip()
                if not text:
                    continue
                try:
                    msg = json.loads(text)
                except ValueError:
                    logger.debug("MCP %s non-JSON output: %s", self.name, text[:200])
                    continue
                if not isinstance(msg, dict):
                    continue
                rid = msg.get("id")
                if "method" in msg:
                    # A server-initiated request/notification: never treat it as a
                    # response to one of our pending ids (ids may collide).
                    if rid is not None:
                        if msg.get("method") == "ping":
                            await self._respond_result(rid, {})
                        else:
                            # sampling/roots etc.: refuse politely.
                            await self._respond_error(rid, -32601, "method not supported by rbit")
                elif rid is not None and rid in self._pending:
                    fut = self._pending.get(rid)
                    if fut is not None and not fut.done():
                        fut.set_result(msg)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("MCP %s reader stopped", self.name, exc_info=True)
        finally:
            if not self._closed and not self.error:
                self.error = "server closed the connection"
            err = MCPError(self.error or "server closed the connection")
            for fut in list(self._pending.values()):
                if not fut.done():
                    fut.set_exception(err)
            self._pending.clear()

    async def _respond_result(self, rid: Any, result: dict[str, Any]) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None:
            return
        payload = {"jsonrpc": "2.0", "id": rid, "result": result}
        try:
            proc.stdin.write((json.dumps(payload) + "\n").encode("utf-8"))
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass

    async def _respond_error(self, rid: Any, code: int, message: str) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None:
            return
        payload = {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}
        try:
            proc.stdin.write((json.dumps(payload) + "\n").encode("utf-8"))
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass

    async def _stderr_loop(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        try:
            while True:
                line = await proc.stderr.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").rstrip()
                if not text:
                    continue
                self._stderr_tail.append(text)
                if len(self._stderr_tail) > MAX_STDERR_TAIL:
                    del self._stderr_tail[: len(self._stderr_tail) - MAX_STDERR_TAIL]
                logger.debug("MCP %s stderr: %s", self.name, text)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - stderr is diagnostic only
            logger.debug("MCP %s stderr reader stopped: %s", self.name, exc)

    async def _list_tools(self) -> list[MCPTool]:
        tools: list[MCPTool] = []
        cursor: str | None = None
        for _ in range(20):
            params: dict[str, Any] = {}
            if cursor:
                params["cursor"] = cursor
            result = await self._request("tools/list", params, timeout=self.timeout)
            for item in result.get("tools") or []:
                if not isinstance(item, dict):
                    continue
                raw_name = str(item.get("name") or "").strip()
                if not raw_name:
                    continue
                tools.append(
                    MCPTool(
                        server=self.name,
                        raw_name=raw_name,
                        exposed_name="",
                        description=str(item.get("description") or "").strip(),
                        input_schema=item.get("inputSchema") if isinstance(item.get("inputSchema"), dict) else {},
                    )
                )
            cursor = result.get("nextCursor")
            if not cursor:
                break
        return tools

    async def list_resources(self) -> list[dict[str, Any]]:
        try:
            result = await self._request("resources/list", {})
        except MCPError:
            return []
        resources = result.get("resources")
        return resources if isinstance(resources, list) else []

    async def read_resource(self, uri: str) -> dict[str, Any]:
        return await self._request("resources/read", {"uri": uri})

    async def call_tool(self, raw_name: str, arguments: dict[str, Any]) -> MCPCallResult:
        try:
            result = await self._request(
                "tools/call",
                {"name": raw_name, "arguments": arguments or {}},
                timeout=self.call_timeout,
            )
        except Exception as exc:  # noqa: BLE001 - surface tool failures as results
            return MCPCallResult(False, f"MCP {self.name}: call failed", self._format_error(exc))
        text = _content_to_text(result.get("content"))
        structured = result.get("structuredContent")
        if structured is not None:
            try:
                rendered = json.dumps(structured, ensure_ascii=False, indent=2)
            except (TypeError, ValueError):
                rendered = str(structured)
            text = f"{text}\n\n{rendered}".strip() if text else rendered
        if not text:
            text = "(no output)"
        if len(text) > MAX_TOOL_OUTPUT:
            text = text[:MAX_TOOL_OUTPUT] + f"\n… truncated ({len(text) - MAX_TOOL_OUTPUT} chars)"
        is_error = bool(result.get("isError"))
        return MCPCallResult(
            ok=not is_error,
            summary=f"{self.name}/{raw_name} {'error' if is_error else 'ok'}",
            output=text,
        )


def _content_to_text(content: Any) -> str:
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if isinstance(item, str):
            parts.append(item)
            continue
        if not isinstance(item, dict):
            parts.append(str(item))
            continue
        kind = item.get("type")
        if kind == "text":
            parts.append(str(item.get("text") or ""))
        elif kind == "resource":
            resource = item.get("resource") or {}
            text = resource.get("text") if isinstance(resource, dict) else ""
            uri = resource.get("uri") if isinstance(resource, dict) else ""
            parts.append(str(text or f"(resource: {uri})"))
        elif kind == "image":
            parts.append(f"(image: {item.get('mimeType') or 'unknown'})")
        else:
            parts.append(json.dumps(item, ensure_ascii=False))
    return "\n".join(part for part in parts if part)


def _which(command: str) -> str | None:
    import shutil

    if os.path.isabs(command) or os.sep in command or (os.altsep and os.altsep in command):
        return command
    return shutil.which(command)


def _sanitize(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_]", "_", value).strip("_")
    return cleaned or "tool"


def _normalize_configs(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    raw = config.get("mcp")
    if not isinstance(raw, dict) or not raw:
        raw = config.get("mcp_servers")
    if not isinstance(raw, dict):
        return {}
    normalized: dict[str, dict[str, Any]] = {}
    for name, entry in raw.items():
        if not name:
            continue
        if isinstance(entry, str):
            normalized[str(name)] = {"type": "local", "command": entry}
        elif isinstance(entry, dict):
            normalized[str(name)] = dict(entry)
    return normalized


class MCPManager:
    """Owns every configured MCP server and exposes their tools to the executor."""

    def __init__(
        self,
        workspace: Path,
        config: dict[str, Any],
        *,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self.configs = _normalize_configs(config)
        tokens = config.get("mcp_oauth")
        if isinstance(tokens, dict):
            for name, token in tokens.items():
                entry = self.configs.get(str(name))
                if not isinstance(entry, dict) or not token:
                    continue
                entry = dict(entry)
                headers = dict(entry.get("headers") or {})
                headers.setdefault("Authorization", f"Bearer {token}")
                entry["headers"] = headers
                self.configs[str(name)] = entry
        self.status: dict[str, dict[str, Any]] = {}
        self.tools: dict[str, MCPTool] = {}
        self._tool_owner: dict[str, MCPClient] = {}
        self._raw_names: dict[str, str] = {}
        self._http_client = http_client
        self.clients: dict[str, MCPClient] = {}
        for name, cfg in self.configs.items():
            try:
                client = MCPClient(name, cfg, self.workspace, http_client=http_client)
                client.global_config = config
            except Exception as exc:  # noqa: BLE001 - one bad server must not break the rest
                self.status[name] = {
                    "connected": False,
                    "tools": 0,
                    "error": f"invalid configuration: {exc}",
                    "transport": "invalid",
                }
                continue
            self.clients[name] = client
            self.status[name] = {
                "connected": False,
                "tools": 0,
                "error": "",
                "transport": client.transport,
            }

    @classmethod
    def from_config(
        cls, config: dict[str, Any], workspace: Path, *, http_client: httpx.AsyncClient | None = None
    ) -> MCPManager:
        return cls(workspace, config, http_client=http_client)

    @property
    def tool_names(self) -> set[str]:
        return set(self.tools)

    @property
    def tool_count(self) -> int:
        return len(self.tools)

    @property
    def configured_count(self) -> int:
        return len(self.clients)

    async def start(self) -> None:
        if not self.clients:
            return
        await asyncio.gather(*(self._start_one(client) for client in self.clients.values()), return_exceptions=True)

    async def _start_one(self, client: MCPClient) -> None:
        t0 = time.monotonic()
        await client.start()
        status = self.status.setdefault(client.name, {})
        status.update(
            {
                "connected": client.connected,
                "tools": len(client.tools),
                "error": client.error,
                "transport": client.transport,
                "ms": int((time.monotonic() - t0) * 1000),
                "server": (client.server_info or {}).get("name", ""),
            }
        )
        if not client.connected:
            return
        for tool in client.tools:
            exposed = self._unique_name(client.name, tool.raw_name)
            tool.exposed_name = exposed
            self.tools[exposed] = tool
            self._tool_owner[exposed] = client
            self._raw_names[exposed] = tool.raw_name

    def _unique_name(self, server: str, tool: str) -> str:
        base = f"{_sanitize(server)}_{_sanitize(tool)}"
        candidate = base
        counter = 2
        while candidate in self.tools:
            candidate = f"{base}_{counter}"
            counter += 1
        return candidate

    def schemas(self) -> list[dict[str, Any]]:
        return [tool.schema() for tool in self.tools.values()]

    async def call(self, exposed_name: str, arguments: dict[str, Any]) -> MCPCallResult:
        client = self._tool_owner.get(exposed_name)
        raw_name = self._raw_names.get(exposed_name)
        if client is None or raw_name is None:
            return MCPCallResult(False, f"Unknown MCP tool: {exposed_name}", "The tool is no longer available.")
        result = await client.call_tool(raw_name, arguments)
        if not result.ok and client.error:
            result.output = f"{result.output}\n\n{client.error}".strip()
        return result

    async def list_resources(self) -> dict[str, list[dict[str, Any]]]:
        result: dict[str, list[dict[str, Any]]] = {}
        for name, client in self.clients.items():
            if client.connected:
                result[name] = await client.list_resources()
        return result

    async def read_resource(self, server: str, uri: str) -> dict[str, Any]:
        client = self.clients.get(server)
        if client is None:
            raise MCPError(f"unknown MCP server: {server}")
        return await client.read_resource(uri)

    def summary_lines(self) -> list[str]:
        lines: list[str] = []
        for name in self.clients:
            status = self.status.get(name) or {}
            if status.get("connected"):
                count = int(status.get("tools") or 0)
                lines.append(f"• {name} — {count} tool{'s' if count != 1 else ''}")
            else:
                error = status.get("error") or "not connected"
                lines.append(f"• {name} — {error}")
        return lines

    async def close(self) -> None:
        await asyncio.gather(*(client.close() for client in self.clients.values()), return_exceptions=True)
        self.tools.clear()
        self._tool_owner.clear()
        self._raw_names.clear()

    def terminate(self) -> None:
        for client in self.clients.values():
            client.terminate()
