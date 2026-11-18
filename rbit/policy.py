"""Policy layer: hook execution limits, MCP tool risk, model capabilities.

Three related decisions live here because they all answer the same question —
"is this safe and sane to do right now?":

* **Hooks** run arbitrary user code on every tool call. A hung hook would freeze
  a turn forever, so each one gets a timeout, an output cap and a call budget.
  Unknown hooks must be approved before they run for the first time: otherwise
  cloning a repo silently executes its hooks.
* **MCP tools** arrive from third-party servers with no reliable description of
  what they touch. `classify_mcp_tool()` maps a tool to read / write /
  destructive using its annotations when present and its name otherwise, so the
  permission engine can require approval for the dangerous ones.
* **Model capabilities** decide whether a model can actually run a turn, and the
  routing policy picks among candidates by cost or quality within a budget.
"""

from __future__ import annotations

import asyncio
import math
import re
from dataclasses import dataclass, field
from typing import Any

#: Hard ceiling on a single hook invocation, in seconds.
DEFAULT_HOOK_TIMEOUT = 8.0

#: Cap on a hook's returned context, so a runaway hook cannot blow up a prompt.
MAX_HOOK_OUTPUT_CHARS = 8000

#: Max hook invocations per session before the rest are skipped.
MAX_HOOK_CALLS = 200


@dataclass
class HookPolicy:
    """Limits and approval state for plugin hooks."""

    timeout: float = DEFAULT_HOOK_TIMEOUT
    max_output_chars: int = MAX_HOOK_OUTPUT_CHARS
    max_calls: int = MAX_HOOK_CALLS
    require_approval: bool = True
    approved: set[str] = field(default_factory=set)
    disabled: set[str] = field(default_factory=set)
    calls: dict[str, int] = field(default_factory=dict)
    failures: dict[str, int] = field(default_factory=dict)
    #: Names skipped because they were never approved.
    pending: set[str] = field(default_factory=set)

    @classmethod
    def from_config(cls, config: dict[str, Any] | None) -> HookPolicy:
        config = config or {}
        block = config.get("hooks")
        block = block if isinstance(block, dict) else {}
        approved = block.get("approved")
        disabled = block.get("disabled")
        timeout = block.get("timeout_s")
        # Default is "trusted": a plugin only runs because the user put its path
        # in their own config. Code that arrives another way — `rbit plugin
        # install`, a cloned repo — is kept *out* of the config until explicitly
        # enabled, so approval is enforced at the door rather than on every call.
        # Set `hooks.require_approval: true` to also gate config-listed plugins.
        return cls(
            timeout=float(timeout) if isinstance(timeout, (int, float)) and timeout > 0 else DEFAULT_HOOK_TIMEOUT,
            require_approval=block.get("require_approval") is True,
            approved={str(x) for x in approved} if isinstance(approved, list) else set(),
            disabled={str(x) for x in disabled} if isinstance(disabled, list) else set(),
        )

    def allows(self, name: str) -> bool:
        if name in self.disabled:
            return False
        if not self.require_approval:
            return True
        if name in self.approved:
            return True
        self.pending.add(name)
        return False

    def budget_left(self, name: str) -> bool:
        return self.calls.get(name, 0) < self.max_calls

    def note_call(self, name: str) -> None:
        self.calls[name] = self.calls.get(name, 0) + 1

    def note_failure(self, name: str) -> None:
        self.failures[name] = self.failures.get(name, 0) + 1

    def to_config_patch(self) -> dict[str, Any]:
        """The `hooks` block to persist after approving/blocking something."""
        return {
            "require_approval": self.require_approval,
            "timeout_s": self.timeout,
            "approved": sorted(self.approved),
            "disabled": sorted(self.disabled),
        }


def _clip_context(context: dict[str, Any], limit: int) -> dict[str, Any]:
    """Keeps a hook's output bounded without touching keys the engine needs."""
    clipped: dict[str, Any] = {}
    for key, value in context.items():
        if isinstance(value, str) and len(value) > limit:
            clipped[key] = value[:limit] + f"… [truncated by hook policy at {limit}]"
        else:
            clipped[key] = value
    return clipped


async def run_hook_safely(
    name: str,
    callback: Any,
    context: dict[str, Any],
    policy: HookPolicy,
    on_failure: Any = None,
) -> dict[str, Any]:
    """Invokes one hook under the policy's limits; never raises."""
    if not policy.allows(name):
        return context
    if not policy.budget_left(name):
        return context

    policy.note_call(name)
    try:
        result = callback(context)
        if hasattr(result, "__await__"):
            result = await asyncio.wait_for(result, timeout=policy.timeout)
        if isinstance(result, dict):
            merged = dict(context)
            merged.update(result)
            return _clip_context(merged, policy.max_output_chars)
    except asyncio.TimeoutError:
        policy.note_failure(name)
        if on_failure is not None:
            on_failure(name, f"timeout after {policy.timeout:.1f}s")
    except Exception as exc:  # noqa: BLE001 - hooks are untrusted
        policy.note_failure(name)
        if on_failure is not None:
            on_failure(name, f"{type(exc).__name__}: {exc}")
    return context


# --- MCP tool risk --------------------------------------------------------

#: Name fragments that imply a tool touches data rather than reading it.
_SEP = r"(?:^|[^a-z0-9])"
_END = r"(?:$|[^a-z0-9])"
_WRITE_HINTS = re.compile(
    _SEP + r"(write|create|insert|update|put|post|send|upload|delete|remove|drop|"
    r"truncate|patch|merge|commit|push|execute|run|exec|apply|set|move|rename|"
    r"save|deploy|publish|grant|revoke)" + _END,
    re.IGNORECASE,
)
_READ_HINTS = re.compile(
    _SEP + r"(get|list|read|fetch|search|query|find|describe|show|info|status|count|head)" + _END,
    re.IGNORECASE,
)

#: Never auto-approved: names that can destroy data or take over a system.
_DESTRUCTIVE_HINTS = re.compile(
    _SEP + r"(delete|drop|truncate|destroy|purge|wipe|revoke|terminate|kill|"
    r"reset|overwrite|force|exec|shell|sudo|deploy|rm)" + _END,
    re.IGNORECASE,
)


def classify_mcp_tool(tool: Any, name: str = "") -> str:
    """Returns 'read', 'write' or 'destructive' for an MCP tool.

    Annotations win when the server provides them (MCP defines `readOnlyHint`
    and `destructiveHint`); otherwise the tool name is the only signal, and it is
    treated pessimistically.
    """
    if isinstance(tool, dict):
        label = name or str(tool.get("name") or "")
        annotations = tool.get("annotations")
    else:
        label = name or str(getattr(tool, "name", "") or "")
        annotations = getattr(tool, "annotations", None)
    if not isinstance(annotations, dict):
        annotations = {}
    # MCP nests hints under `annotations` but some servers use a flat block.
    if not annotations and isinstance(tool, dict):
        flat = tool.get("inputSchema")
        if isinstance(flat, dict) and isinstance(flat.get("annotations"), dict):
            annotations = flat["annotations"]
    if annotations.get("destructiveHint") is True:
        return "destructive"
    if annotations.get("readOnlyHint") is True:
        return "read"
    if _DESTRUCTIVE_HINTS.search(label):
        return "destructive"
    if _WRITE_HINTS.search(label):
        return "write"
    if _READ_HINTS.search(label):
        return "read"
    # Unknown shape: assume it writes, so a prompt is required.
    return "write"


def mcp_action_for(risk: str) -> str:
    return {"read": "mcp_read", "write": "mcp_write", "destructive": "mcp_destructive"}.get(
        risk, "mcp_write"
    )


# --- model capabilities ---------------------------------------------------


@dataclass
class ModelCapabilities:
    """What a model can do, and what a turn requires."""

    model: str
    tools: bool = True
    streaming: bool = True
    context_window: int = 0
    cost_per_1k: float = 0.0
    missing: list[str] = field(default_factory=list)

    @property
    def usable(self) -> bool:
        return not self.missing


def check_capabilities(
    model: str,
    *,
    context_window: int = 0,
    cost_per_1k: float = 0.0,
    supports_tools: bool = True,
    required_context: int = 0,
) -> ModelCapabilities:
    """Verifies a model against what the agent needs before switching to it."""
    caps = ModelCapabilities(
        model=model,
        tools=supports_tools,
        context_window=int(context_window or 0),
        cost_per_1k=float(cost_per_1k or 0.0),
    )
    if not supports_tools:
        caps.missing.append("tool calling")
    if required_context and caps.context_window and required_context > caps.context_window:
        caps.missing.append(
            f"context window {caps.context_window:,} < required {required_context:,}"
        )
    return caps


# --- routing policy -------------------------------------------------------

#: The smallest context an agent turn can realistically work in.
MIN_AGENT_CONTEXT = 32_000

ROUTER_POLICIES = ("balanced", "cheapest", "best_value")


def normalize_router_policy(raw: Any, default: str = "balanced") -> str:
    if isinstance(raw, str):
        key = raw.strip().lower().replace("-", "_")
        aliases = {
            "cheap": "cheapest",
            "budget": "cheapest",
            "quality": "best_value",
            "best": "best_value",
            "auto": "balanced",
        }
        key = aliases.get(key, key)
        if key in ROUTER_POLICIES:
            return key
    return default


@dataclass
class Candidate:
    """A model that could serve a turn."""

    model: str
    cost_per_1k: float = 0.0
    context_window: int = 0
    tools: bool = True

    def score_value(self, required_context: int = 0) -> float:
        """Higher is better: quality proxy per dollar.

        Context window is a poor quality proxy on its own — a tiny cheap model
        with a big window would win — so the curve is logarithmic and floored.
        Models below `MIN_AGENT_CONTEXT` are disqualified outright: an agent turn
        needs room for the tool schemas, the file it is editing and the reply, so
        a 8k model is not a bargain, it is unusable.
        """
        if not self.tools:
            return float("-inf")
        if self.context_window and self.context_window < MIN_AGENT_CONTEXT:
            return float("-inf")
        if required_context and self.context_window and self.context_window < required_context:
            return float("-inf")
        quality = 1.0
        if self.context_window:
            # log2 so 32k -> 0.88, 128k -> 1.0, 200k -> 1.08 (diminishing returns).
            quality = max(0.8, min(1.2, math.log2(max(2, self.context_window)) / 17.0))
        price = self.cost_per_1k if self.cost_per_1k > 0 else 1e-6
        return quality / price


def pick_candidate(
    candidates: list[Candidate],
    policy: str = "balanced",
    *,
    required_context: int = 0,
    max_cost_per_1k: float | None = None,
) -> tuple[Candidate | None, str]:
    """Chooses a model under `policy`; returns (candidate, reason)."""
    usable = [
        c
        for c in candidates
        if c.tools and (not required_context or not c.context_window or c.context_window >= required_context)
    ]
    if max_cost_per_1k is not None:
        affordable = [c for c in usable if c.cost_per_1k <= max_cost_per_1k]
        if affordable:
            usable = affordable
        elif usable:
            return None, f"no model under ${max_cost_per_1k:.4f}/1k fits the context requirement"
    if not usable:
        return None, "no capable model available"

    policy = normalize_router_policy(policy)
    if policy == "cheapest":
        cheapest = min(usable, key=lambda c: c.cost_per_1k)
        return cheapest, f"cheapest capable model (${cheapest.cost_per_1k:.4f}/1k)"
    if policy == "best_value":
        best = max(usable, key=lambda c: c.score_value(required_context))
        return best, f"best quality per dollar (${best.cost_per_1k:.4f}/1k, {best.context_window:,} ctx)"
    # balanced: prefer cheap but not the rock bottom if it has a tiny context.
    ranked = sorted(usable, key=lambda c: (c.context_window >= 32_000, -c.cost_per_1k), reverse=True)
    chosen = ranked[0]
    return chosen, f"balanced pick (${chosen.cost_per_1k:.4f}/1k)"
