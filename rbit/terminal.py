"""Minimal persistent shell used by the `terminal` tool."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path


class TerminalError(RuntimeError):
    pass


class PersistentShell:
    def __init__(self, workspace: Path, timeout: float = 30.0) -> None:
        self.workspace = workspace.resolve()
        self.timeout = timeout
        self.process: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()
        self._marker_counter = 0
        self._buffer = ""

    @staticmethod
    def _command() -> list[str]:
        if os.name == "nt":
            return ["cmd.exe", "/Q", "/K"]
        return [os.environ.get("SHELL") or "/bin/sh", "-i"]

    async def start(self) -> None:
        if self.process is not None and self.process.returncode is None:
            return
        self.process = await asyncio.create_subprocess_exec(
            *self._command(),
            cwd=str(self.workspace),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

    async def run(self, command: str) -> str:
        if not command.strip():
            raise TerminalError("empty command")
        async with self._lock:
            await self.start()
            assert self.process is not None and self.process.stdin is not None and self.process.stdout is not None
            self._marker_counter += 1
            marker = f"__RBIT_DONE_{self._marker_counter}_{os.getpid()}__"
            line = command.rstrip() + "\n"
            if os.name == "nt":
                marker_cmd = f"echo {marker}\n"
            else:
                marker_cmd = f"echo {marker}\n"
            try:
                self.process.stdin.write((line + marker_cmd).encode("utf-8", errors="replace"))
                await self.process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError) as exc:
                raise TerminalError(f"terminal closed: {exc}") from exc
            deadline = asyncio.get_running_loop().time() + self.timeout
            chunks: list[str] = []
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise TerminalError(f"command timed out after {self.timeout:.0f}s")
                try:
                    data = await asyncio.wait_for(self.process.stdout.read(4096), timeout=remaining)
                except asyncio.TimeoutError as exc:
                    raise TerminalError(f"command timed out after {self.timeout:.0f}s") from exc
                if not data:
                    raise TerminalError("terminal exited")
                text = data.decode("utf-8", errors="replace")
                self._buffer += text
                if marker in self._buffer:
                    before, _, after = self._buffer.partition(marker)
                    self._buffer = after.lstrip("\r\n")
                    chunks.append(before)
                    break
                chunks.append(text)
            output = "".join(chunks).replace("\r\n", "\n").replace("\r", "\n")
            lines = [ln for ln in output.splitlines() if not ln.strip().lower().startswith("microsoft windows")]
            return "\n".join(lines).strip()

    async def close(self) -> None:
        process = self.process
        self.process = None
        if process is None:
            return
        if process.returncode is None:
            try:
                if process.stdin is not None:
                    process.stdin.write(b"exit\n")
                    await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError, RuntimeError):
                pass
            try:
                await asyncio.wait_for(process.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                process.kill()
                try:
                    await process.wait()
                except ProcessLookupError:
                    pass
