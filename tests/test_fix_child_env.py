"""Programs the agent starts must not inherit the agent's own LLM credentials."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Any

import pytest

from rbit.tools import sanitized_env

SECRETS = {
    "OPENAI_API_KEY": "sk-openai",
    "OPENROUTER_API_KEY": "sk-or",
    "ANTHROPIC_API_KEY": "sk-ant",
    "RBIT_API_KEY": "sk-rbit",
    "RBIT_SERVER_TOKEN": "tok",
    "PERPLEXITY_API_KEY": "pplx",
    "FIREWORKS_API_KEY": "fw",
    "CEREBRAS_API_KEY": "cb",
}


@pytest.fixture
def secret_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in SECRETS.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("KEEP_ME", "1")


def test_sanitized_env_drops_every_llm_credential_and_keeps_the_rest(secret_env: None) -> None:
    env = sanitized_env()
    assert not set(SECRETS) & set(env)
    assert env["KEEP_ME"] == "1" and "PATH" in {k.upper() for k in env}


@pytest.mark.asyncio
async def test_persistent_terminal_does_not_inherit_the_credentials(
    secret_env: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from rbit.terminal import PersistentShell

    seen: dict[str, Any] = {}

    async def fake_exec(*_args: Any, **kwargs: Any) -> None:
        seen.update(kwargs)
        raise RuntimeError("stop before the handshake")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    with pytest.raises(RuntimeError):
        await PersistentShell(tmp_path).start()

    assert seen["env"] and not set(SECRETS) & set(seen["env"])
    assert seen["env"]["KEEP_ME"] == "1"


def test_project_formatter_does_not_inherit_the_credentials(
    secret_env: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from rbit import formatters

    seen: dict[str, Any] = {}

    def fake_run(*_args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen.update(kwargs)
        return subprocess.CompletedProcess(_args, 0, "", "")

    monkeypatch.setattr(formatters.subprocess, "run", fake_run)
    target = tmp_path / "a.py"
    target.write_text("x=1\n", encoding="utf-8")
    definition = formatters.FormatterDef(name="f", command=["fmt", "$FILE"], extensions=[".py"], environment={"OWN": "1"})

    formatters._run_formatter(target, definition, tmp_path)

    assert not set(SECRETS) & set(seen["env"])
    assert seen["env"]["OWN"] == "1"  # the formatter's own declared variables still apply
