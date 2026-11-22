"""Minimal persistent shell used by the `terminal` tool."""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

from .envscrub import sanitized_env


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
        self._buffer = ""
        env = sanitized_env()
        if os.name != "nt":
            # No prompts in the captured output.
            env["PS1"] = ""
            env["PS2"] = ""
        self.process = await asyncio.create_subprocess_exec(
            *self._command(),
            cwd=str(self.workspace),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
        )
        await self._handshake()

    async def _handshake(self) -> None:
        """Silences echo/prompt and swallows the start-up banner.

        Everything printed before the ready marker (cmd.exe banner, prompts)
        is discarded so it cannot leak into the first command's output.
        """
        process = self.process
        if process is None or process.stdin is None or process.stdout is None:
            return
        marker = f"__RBIT_READY_{os.getpid()}__"
        if os.name == "nt":
            setup = f"@echo off\r\nprompt $S\r\necho {marker}\r\n"
        else:
            setup = f"PS1=''; PS2=''; echo {marker}\n"
        try:
            process.stdin.write(setup.encode("utf-8"))
            await process.stdin.drain()
            seen = ""
            loop = asyncio.get_running_loop()
            deadline = loop.time() + 10.0
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break
                data = await asyncio.wait_for(process.stdout.read(4096), timeout=remaining)
                if not data:
                    break
                seen += data.decode("utf-8", errors="replace")
                idx = seen.rfind(marker)
                if idx != -1 and "\n" in seen[idx:]:
                    self._buffer = seen[idx + len(marker):].lstrip("\r\n")
                    break
        except (asyncio.TimeoutError, BrokenPipeError, ConnectionResetError):
            pass

    async def _reset(self) -> None:
        """Kills the shell (and its children) and drops any buffered output."""
        process = self.process
        self.process = None
        self._buffer = ""
        if process is None or process.returncode is not None:
            return
        if os.name == "nt":
            try:
                await asyncio.to_thread(
                    subprocess.run,
                    ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                    capture_output=True,
                    check=False,
                    timeout=10,
                )
            except Exception:
                pass
        try:
            process.kill()
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(process.wait(), timeout=3.0)
        except (asyncio.TimeoutError, ProcessLookupError):
            pass

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
                    await self._reset()
                    raise TerminalError(f"command timed out after {self.timeout:.0f}s (shell restarted)")
                try:
                    data = await asyncio.wait_for(self.process.stdout.read(4096), timeout=remaining)
                except asyncio.TimeoutError as exc:
                    # Kill the shell so late output cannot leak into the next command.
                    await self._reset()
                    raise TerminalError(f"command timed out after {self.timeout:.0f}s (shell restarted)") from exc
                if not data:
                    await self._reset()
                    raise TerminalError("terminal exited")
                text = data.decode("utf-8", errors="replace")
                self._buffer += text
                if marker in self._buffer:
                    before, _, after = self._buffer.partition(marker)
                    self._buffer = after.lstrip("\r\n")
                    # `_buffer` already holds every chunk since the last marker:
                    # appending the chunks separately duplicated the output.
                    chunks.append(before)
                    break
            output = "".join(chunks).replace("\r\n", "\n").replace("\r", "\n")
            lines = [
                ln
                for ln in output.splitlines()
                if not ln.strip().lower().startswith(("microsoft windows", "(c) microsoft"))
            ]
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
