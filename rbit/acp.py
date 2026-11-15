"""Agent Client Protocol (ACP) server for editor integrations (Zed and others).

Transport: JSON-RPC 2.0 over stdio, one JSON object per line.  The default
runner drives `AgentEngine` and streams assistant text as
`session/update` notifications.  A custom runner can be injected in tests.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

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
        executor = ToolExecutor(workspace=workspace, session=session, storage=storage)
        engine = AgentEngine(workspace=workspace, executor=executor, storage=storage)
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
            agent_mode="build",
            model_id=str(storage.load_config().get("model", "rbit/smart-local")),
            on_tool_start=_noop,
            on_tool_end=_noop,
            on_status=_status,
            on_stream_chunk=_chunk,
        )
        storage.save_session(session)
        if not reply.content and reply.content != "":
            return "end_turn"
        return "end_turn"

    async def handle(self, message: dict[str, Any], notify: Callable[[dict[str, Any]], Awaitable[None]]) -> dict[str, Any] | None:
        method = message.get("method")
        request_id = message.get("id")
        params = message.get("params") or {}
        if method == "initialize":
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "protocolVersion": int(params.get("protocolVersion") or 1),
                    "agentCapabilities": {
                        "loadSession": False,
                        "promptCapabilities": {
                            "image": False,
                            "audio": False,
                            "embeddedContext": True,
                        },
                    },
                    "agentInfo": {"name": "rbit", "version": "1.3.1"},
                },
            }
        if method == "session/new":
            session_id = uuid4().hex
            cwd = Path(params.get("cwd") or self.workspace)
            self.sessions[session_id] = Session(id=session_id)
            self.session_workspaces[session_id] = cwd
            return {"jsonrpc": "2.0", "id": request_id, "result": {"sessionId": session_id}}
        if method == "session/load":
            session_id = str(params.get("sessionId") or "")
            if session_id not in self.sessions:
                self.sessions[session_id] = Session(id=session_id)
            self.session_workspaces.setdefault(session_id, Path(params.get("cwd") or self.workspace))
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
        if method in ("session/set_mode", "session/set_model"):
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
                continue
            response = await self.handle(message, notify)
            if response is not None:
                await _write(response)


async def run_acp(workspace: Path | None = None) -> int:
    server = ACPServer(workspace=workspace)
    await server.serve()
    return 0
