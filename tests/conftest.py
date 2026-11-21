"""Shared pytest fixtures: isolated workspace and a scrubbed provider environment."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

_PROVIDER_ENV_PREFIXES = ("RBIT_", "OPENAI_", "OPENROUTER_", "ANTHROPIC_", "DEEPSEEK_")


@pytest.fixture(autouse=True)
def clean_provider_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Removes API keys and provider overrides so tests never hit the network by accident."""
    for key in list(os.environ):
        if key.startswith(_PROVIDER_ENV_PREFIXES):
            monkeypatch.delenv(key, raising=False)


@pytest.fixture(autouse=True)
def isolated_trust(monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory, clean_provider_env: None) -> None:
    """Keeps tests away from the user's ~/.rbit/trust.json and out of the trust prompts.

    Project configs written by tests may contain MCP servers, plugins, hooks... which a real
    run would ignore until the user trusts them. Most tests are about those features, so they
    run with RBIT_TRUST_ALL=1; tests of the trust logic itself delete it (tests/test_fix_trust.py).
    """
    monkeypatch.setenv("RBIT_TRUST_STORE", str(tmp_path_factory.mktemp("trust") / "trust.json"))
    monkeypatch.setenv("RBIT_TRUST_ALL", "1")


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """Temporary workspace with an empty .rbit state directory."""
    (tmp_path / ".rbit").mkdir()
    return tmp_path
