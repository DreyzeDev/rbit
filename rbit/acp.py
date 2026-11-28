"""Agent Client Protocol (ACP) server for editor integrations (Zed and others).

Transport: JSON-RPC 2.0 over stdio, one JSON object per line.  The default
runner drives `AgentEngine` and streams assistant text as
`session/update` notifications.  A custom runner can be injected in tests.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

from . import __version__
from .agent import AgentEngine
from .models import ChatMessage, Session, short_id
from .storage import StorageManager

logger = logging.getLogger("rbit.acp")

Runner = Callable[[str, str, Callable[[str], Awaitable[None]]], Awaitable[str]]


class ACPServer:
    def __init__(self, runner: Runner | None = None, workspace: Path | None = None) -> None:
        self.workspace = (workspace or Path.cwd()).resolve()
        self.runner = runner or self._default_runner
        self.sessions: dict[str, Session] = {}
        self.session_workspaces: dict[str, Path] = {}
        self.cancelled: set[str] = set()
        self.modes: dict[str, str] = {}
        self.models: dict[str, str] = {}

    async def _default_runner(
        self,
        prompt: str,
        session_id: str,
        send_chunk: Callable[[str], Awaitable[None]],
    ) -> str:
        from .tools import ToolExecutor

        session = self.sessions[session_id]
        workspace = self.session_workspaces.get(session_id, self.workspace)
        storage = StorageManager(workspace)
        cfg = storage.load_config()

        def get_cfg() -> dict[str, Any]:
            return cfg

        executor = ToolExecutor(workspace=workspace, session=session, storage=storage, get_config=get_cfg)
        # Permissions are NOT granted silently: only an explicit opt-in
        # (RBIT_ACP_ALLOW_ALL=1 or config acp_allow_all) auto-approves tools.
        allow_all = bool(cfg.get("acp_allow_all")) or os.environ.get("RBIT_ACP_ALLOW_ALL") == "1"
        if allow_all:
            executor.always_allow_bash = True

            async def _allow(_tool: str, _detail: str) -> bool:
                return True

            executor.permission_cb = _allow
        else:

            async def _deny(_tool: str, _detail: str) -> bool:
                return False

            executor.permission_cb = _deny
        engine = AgentEngine(workspace=workspace, executor=executor, get_config=get_cfg, storage=storage)
        user_msg = ChatMessage(id=short_id(), role="user", content=prompt)
        session.messages.append(user_msg)

        async def _noop(_record: Any) -> None:
            return None

        async def _status(_text: str) -> None:
            return None

        async def _chunk(text: str) -> None:
            if text:
                await send_chunk(text)

        reply = await engine.run_turn(
            session=session,
            user_msg=user_msg,
            agent_mode=self.modes.get(session_id, "build"),
            model_id=self.models.get(session_id) or str(storage.load_config().get("model", "rbit/smart-local")),
            on_tool_start=_noop,
            on_tool_end=_noop,
            on_status=_status,
            on_stream_chunk=_chunk,
        )
        session.messages.append(reply)
        storage.save_session(session)
        if getattr(reply, "interrupted", False):
            return "cancelled"
        content = reply.content or ""
        if content.startswith("Ошибка API") or content.startswith("**Budget guard"):
            return "refusal"
        if "max_tokens" in content[:200] and "cut off" in content[:400]:
            return "max_tokens"
        return "end_turn"

    async def handle(self, message: dict[str, Any], notify: Callable[[dict[str, Any]], Awaitable[None]]) -> dict[str, Any] | None:
        request_id = message.get("id")
        try:
            return await self._handle(message, notify)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # never crash the server on one bad request
            logger.exception("acp handler failed")
            if request_id is None:
                return None
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32603, "message": f"internal error: {type(exc).__name__}: {exc}"},
            }

    async def _handle(self, message: dict[str, Any], notify: Callable[[dict[str, Any]], Awaitable[None]]) -> dict[str, Any] | None:
        method = message.get("method")
        request_id = message.get("id")
        params = message.get("params")
        if params is None:
            params = {}
        if not isinstance(params, dict):
            if request_id is None:
                return None
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32602, "message": "params must be an object"},
            }
        if method == "initialize":
            try:
                protocol_version = int(params.get("protocolVersion") or 1)
            except (TypeError, ValueError):
                protocol_version = 1
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "protocolVersion": protocol_version,
                    "agentCapabilities": {
                        "loadSession": False,
                        "promptCapabilities": {
                            "image": False,
                            "audio": False,
                            "embeddedContext": True,
                        },
                    },
                    "agentInfo": {"name": "rbit", "version": __version__},
                },
            }
        if method == "session/new":
            session_id = uuid4().hex
            cwd = Path(params.get("cwd") or self.workspace)
            self.sessions[session_id] = Session(id=session_id)
            self.session_workspaces[session_id] = cwd
            self.modes[session_id] = "build"
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "sessionId": session_id,
                    "modes": {
                        "currentModeId": "build",
                        "availableModes": [
                            {"id": "build", "name": "Build", "description": "Full access"},
                            {"id": "plan", "name": "Plan", "description": "Read-only planning"},
                        ],
                    },
                },
            }
        if method == "session/load":
            session_id = str(params.get("sessionId") or "")
            if session_id not in self.sessions:
                self.sessions[session_id] = Session(id=session_id)
            self.session_workspaces.setdefault(session_id, Path(params.get("cwd") or self.workspace))
            for stored in self.sessions[session_id].messages:
                if stored.role not in ("user", "assistant") or not stored.content:
                    continue
                await notify(
                    {
                        "jsonrpc": "2.0",
                        "method": "session/update",
                        "params": {
                            "sessionId": session_id,
                            "update": {
                                "sessionUpdate": "agent_message_chunk" if stored.role == "assistant" else "user_message_chunk",
                                "content": {"type": "text", "text": stored.content},
                            },
                        },
                    }
                )
            return {"jsonrpc": "2.0", "id": request_id, "result": None}
        if method == "session/prompt":
            session_id = str(params.get("sessionId") or "")
            if session_id not in self.sessions:
                return {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32602, "message": "unknown session"},
                }
            prompt = self._extract_prompt(params.get("prompt"))
            if not prompt:
                return {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32602, "message": "empty prompt"},
                }
            self.cancelled.discard(session_id)

            async def _send(text: str) -> None:
                if session_id in self.cancelled:
                    return
                await notify(
                    {
                        "jsonrpc": "2.0",
                        "method": "session/update",
                        "params": {
                            "sessionId": session_id,
                            "update": {
                                "sessionUpdate": "agent_message_chunk",
                                "content": {"type": "text", "text": text},
                            },
                        },
                    }
                )

            stop_reason = await self.runner(prompt, session_id, _send)
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"stopReason": "cancelled" if session_id in self.cancelled else stop_reason},
            }
        if method == "session/cancel":
            session_id = str(params.get("sessionId") or "")
            self.cancelled.add(session_id)
            return None
        if method == "session/set_mode":
            session_id = str(params.get("sessionId") or "")
            mode = str(params.get("modeId") or params.get("mode") or "build").lower()
            if mode not in ("build", "plan"):
                return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32602, "message": "unknown mode"}}
            self.modes[session_id] = mode
            return {"jsonrpc": "2.0", "id": request_id, "result": None}
        if method == "session/set_model":
            session_id = str(params.get("sessionId") or "")
            model_id = str(params.get("modelId") or params.get("model") or "")
            if session_id and model_id:
                self.models[session_id] = model_id
            return {"jsonrpc": "2.0", "id": request_id, "result": None}
        if request_id is not None:
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32601, "message": f"method not found: {method}"},
            }
        return None

    @staticmethod
    def _extract_prompt(parts: Any) -> str:
        if isinstance(parts, str):
            return parts.strip()
        if not isinstance(parts, list):
            return ""
        texts: list[str] = []
        for part in parts:
            if isinstance(part, dict) and part.get("type") == "text":
                text = str(part.get("text") or "").strip()
                if text:
                    texts.append(text)
        return "\n".join(texts).strip()

    async def serve(self, stdin: Any = None, stdout: Any = None) -> None:
        reader = stdin or sys.stdin
        writer = stdout or sys.stdout
        write_lock = asyncio.Lock()

        async def notify(payload: dict[str, Any]) -> None:
            await _write(payload)

        async def _write(payload: dict[str, Any]) -> None:
            async with write_lock:
                writer.write(json.dumps(payload, ensure_ascii=False) + "\n")
                writer.flush()

        prompt_tasks: dict[str, asyncio.Task[Any]] = {}
        all_tasks: set[asyncio.Task[Any]] = set()

        async def _run_request(message: dict[str, Any], sid: str) -> None:
            request_id = message.get("id")
            try:
                response = await self.handle(message, notify)
            except asyncio.CancelledError:
                response = {"jsonrpc": "2.0", "id": request_id, "result": {"stopReason": "cancelled"}}
            if response is not None:
                await _write(response)

        while True:
            line = await asyncio.to_thread(reader.readline)
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except ValueError:
                await _write({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}})
                continue
            if not isinstance(message, dict):
                await _write({"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "invalid request"}})
                continue
            method = message.get("method")
            params = message.get("params")
            if method == "session/cancel" and isinstance(params, dict):
                sid = str(params.get("sessionId") or "")
                self.cancelled.add(sid)
                running = prompt_tasks.get(sid)
                if running is not None and not running.done():
                    running.cancel()
                continue
            if method == "session/prompt" and isinstance(params, dict):
                sid = str(params.get("sessionId") or "")
                previous = prompt_tasks.get(sid)
                if previous is not None and not previous.done():
                    await _write(
                        {
                            "jsonrpc": "2.0",
                            "id": message.get("id"),
                            "error": {"code": -32602, "message": "a prompt is already running for this session"},
                        }
                    )
                    continue
                task = asyncio.create_task(_run_request(message, sid))
                prompt_tasks[sid] = task
                all_tasks.add(task)
                task.add_done_callback(all_tasks.discard)
                continue
            response = await self.handle(message, notify)
            if response is not None:
                await _write(response)
        if all_tasks:
            await asyncio.gather(*list(all_tasks), return_exceptions=True)


async def run_acp(workspace: Path | None = None) -> int:
    for stream in (sys.stdin, sys.stdout):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except Exception:
            pass
    server = ACPServer(workspace=workspace)
    await server.serve()
    return 0
