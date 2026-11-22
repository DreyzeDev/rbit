"""Environment for programs the agent starts: without the agent's own LLM credentials."""

from __future__ import annotations

import os

_SENSITIVE_ENV_NAMES = frozenset(
    {
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "OPENROUTER_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "GROQ_API_KEY",
        "MISTRAL_API_KEY",
        "DEEPSEEK_API_KEY",
        "XAI_API_KEY",
        "COHERE_API_KEY",
        "TOGETHER_API_KEY",
        "AZURE_OPENAI_API_KEY",
        "HF_TOKEN",
        "HUGGINGFACE_API_KEY",
        "PERPLEXITY_API_KEY",
        "FIREWORKS_API_KEY",
        "CEREBRAS_API_KEY",
        "NVIDIA_API_KEY",
        "REPLICATE_API_TOKEN",
        "SAMBANOVA_API_KEY",
    }
)


def sanitized_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """Environment for child processes with the agent's own LLM credentials removed."""
    source = os.environ if base is None else base
    clean: dict[str, str] = {}
    for key, value in source.items():
        upper = key.upper()
        if upper in _SENSITIVE_ENV_NAMES:
            continue
        if upper.startswith("RBIT_") and upper.endswith(("_KEY", "_TOKEN", "_SECRET", "_PASSWORD")):
            continue
        clean[key] = value
    return clean
