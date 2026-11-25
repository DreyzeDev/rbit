"""Smart model router: picks a tier (fast/balanced/deep) for a task.

The blueprint wants routing by task complexity, a separate model per role, and —
crucially — that the choice is *explainable* and never silently swaps the model
mid-task. So this module only ever produces a recommendation with a reason; the
caller decides whether to honour it, and an explicit user pin always wins.

Nothing here performs I/O or calls a provider: it takes the task text, the
candidate models and the config, and returns a decision.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .policy import (
    Candidate,
    check_capabilities,
    normalize_router_policy,
    pick_candidate,
)

#: Task tiers, cheapest/fastest first.
TIERS: tuple[str, ...] = ("fast", "balanced", "deep")

DEEP_ALIASES = {"deep", "best", "quality", "max", "heavy"}
FAST_ALIASES = {"fast", "cheap", "small", "quick", "light"}


@dataclass
class RoutingDecision:
    """Outcome of a routing pass, with the reasoning kept for display."""

    model: str
    tier: str
    reason: str
    #: True when the user pinned the model, so the router must not override it.
    pinned: bool = False
    #: True when routing fell back to "keep the current model".
    fallback: bool = False

    def describe(self) -> str:
        if self.pinned:
            return f"{self.model} (pinned by you)"
        return f"{self.model} [{self.tier}] — {self.reason}"


#: Signals that a task needs the deep tier: architectural or cross-cutting work.
_DEEP_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b(refactor|re-?architect|redesign|migrat|rewrite)\w*", re.IGNORECASE),
    re.compile(r"\b(architecture|design|blueprint|specification|spec)\b", re.IGNORECASE),
    re.compile(r"\b(debug|diagnose|root cause|race condition|deadlock|memory leak)\w*", re.IGNORECASE),
    re.compile(r"\b(security|vulnerab|exploit|audit|threat model)\w*", re.IGNORECASE),
    re.compile(r"\b(optimize|performance|profil|benchmark)\w*", re.IGNORECASE),
    re.compile(r"\b(concurrency|distributed|transaction|consisten\w+)\b", re.IGNORECASE),
    re.compile(r"(архитектур|рефактор|переписать|спроектир|миграц|утечк|дедлок)", re.IGNORECASE),
)

#: Signals for the fast tier: tiny, mechanical, lookup-shaped requests.
_FAST_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^\s*(что|what|who|где|where|когда|when|почему|why)\b", re.IGNORECASE),
    re.compile(r"\b(typo|опечатк|переименуй|rename|format|форматир)\w*", re.IGNORECASE),
    re.compile(r"\b(explain|объясни|покажи|show me|summar\w+|кратко)\b", re.IGNORECASE),
    re.compile(r"\b(commit message|коммит|changelog)\b", re.IGNORECASE),
)

#: Multi-step / multi-file work that at least needs the balanced tier.
_SCOPE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b(all files|every file|across the project|по всему проекту|везде)\b", re.IGNORECASE),
    re.compile(r"\b(test|tests|pytest|coverage|ci)\b", re.IGNORECASE),
    re.compile(r"\b(fix|исправ|почин|добав|implement|реализ)\w*", re.IGNORECASE),
)


def classify_task(text: str) -> tuple[str, str]:
    """Returns (tier, short human reason) for a prompt."""
    body = (text or "").strip()
    if not body:
        return "balanced", "empty prompt"

    deep_hits = [p.pattern for p in _DEEP_PATTERNS if p.search(body)]
    if deep_hits or len(body) > 2000:
        reason = "large prompt" if len(body) > 2000 else "architectural/debugging work"
        return "deep", reason

    fast_hits = [p.pattern for p in _FAST_PATTERNS if p.search(body)]
    scope_hits = [p.pattern for p in _SCOPE_PATTERNS if p.search(body)]
    if fast_hits and not scope_hits and len(body) < 400:
        return "fast", "small lookup/mechanical request"
    if scope_hits:
        return "balanced", "multi-step implementation work"
    if len(body) < 120:
        return "fast", "short request"
    return "balanced", "default"


def _tier_map(config: dict[str, Any] | None) -> dict[str, str]:
    """Reads `model_router.tiers` (tier -> model id) from the config."""
    config = config or {}
    block = config.get("model_router")
    if not isinstance(block, dict):
        return {}
    tiers = block.get("tiers")
    if not isinstance(tiers, dict):
        return {}
    mapping: dict[str, str] = {}
    for tier, model in tiers.items():
        if isinstance(tier, str) and isinstance(model, str) and model.strip():
            mapping[tier.strip().lower()] = model.strip()
    return mapping


def _router_enabled(config: dict[str, Any] | None) -> bool:
    config = config or {}
    block = config.get("model_router")
    if isinstance(block, dict):
        return bool(block.get("enabled"))
    return bool(block)


def router_enabled(config: dict[str, Any] | None) -> bool:
    """True when `model_router` is switched on in the config.

    The agent consults this before routing at all: a disabled router must leave the
    user's selected model alone (even a `model_pinned` entry), so routing stays opt-in.
    """
    return _router_enabled(config)


def _model_available(model: str, available: list[str] | None) -> bool:
    if not available:
        return True
    return model in available


def _role_map(config: dict[str, Any] | None) -> dict[str, str]:
    """Reads `model_router.roles` (plan/build/review/subagent -> model id)."""
    config = config or {}
    block = config.get("model_router")
    if not isinstance(block, dict):
        return {}
    roles = block.get("roles")
    if not isinstance(roles, dict):
        return {}
    mapping: dict[str, str] = {}
    for role, model in roles.items():
        if isinstance(role, str) and isinstance(model, str) and model.strip():
            mapping[role.strip().lower()] = model.strip()
    return mapping


def failover_candidates(current_model: str, config: dict[str, Any] | None) -> list[str]:
    """Ordered fallback models from `model_router.fallback_models` and configured tiers."""
    config = config or {}
    block = config.get("model_router")
    block = block if isinstance(block, dict) else {}
    seen: set[str] = {current_model}
    out: list[str] = []
    raw_list = block.get("fallback_models")
    if isinstance(raw_list, list):
        for item in raw_list:
            m = str(item or "").strip()
            if m and m not in seen:
                seen.add(m)
                out.append(m)
    for tier in ("balanced", "fast", "deep"):
        m = _tier_map(config).get(tier)
        if m and m not in seen:
            seen.add(m)
            out.append(m)
    return out


def route(
    text: str,
    current_model: str,
    config: dict[str, Any] | None = None,
    available_models: list[str] | None = None,
    role: str | None = None,
) -> RoutingDecision:
    """Recommends a model for `text`.

    Precedence, in order:
    1. a user pin (`model_router.pin` or `model_pinned`) always wins;
    2. routing must be enabled — otherwise the current model is kept;
    3. the tier's configured model, if it is actually available;
    4. otherwise keep the current model and say why.
    """
    config = config or {}
    block = config.get("model_router")
    block = block if isinstance(block, dict) else {}

    pin = block.get("pin") or config.get("model_pinned")
    if isinstance(pin, str) and pin.strip():
        return RoutingDecision(
            model=pin.strip(),
            tier="pinned",
            reason="pinned in config",
            pinned=True,
        )

    # A task already in progress must not switch models underneath the user.
    if not _router_enabled(config):
        return RoutingDecision(
            model=current_model,
            tier="current",
            reason="router disabled",
            fallback=True,
        )

    if role:
        role_target = _role_map(config).get(role.strip().lower())
        if role_target and _model_available(role_target, available_models):
            return RoutingDecision(
                model=role_target,
                tier=f"role:{role.strip().lower()}",
                reason=f"configured for '{role.strip().lower()}' role",
            )

    tier, reason = classify_task(text)
    mapping = _tier_map(config)
    target = mapping.get(tier)
    if not target:
        # Fall back down the tiers: deep -> balanced -> fast.
        order = {"deep": ("balanced", "fast"), "balanced": ("fast", "deep"), "fast": ("balanced",)}
        for fallback_tier in order.get(tier, ()):
            candidate = mapping.get(fallback_tier)
            if candidate:
                target = candidate
                reason = f"{reason}; '{tier}' tier not configured, using '{fallback_tier}'"
                tier = fallback_tier
                break
    if not target:
        return RoutingDecision(
            model=current_model,
            tier="current",
            reason=f"{reason}; no tiers configured in `model_router.tiers`",
            fallback=True,
        )
    if not _model_available(target, available_models):
        return RoutingDecision(
            model=current_model,
            tier="current",
            reason=f"{reason}; '{target}' is not available right now",
            fallback=True,
        )
    if target == current_model:
        return RoutingDecision(model=target, tier=tier, reason=reason)
    return RoutingDecision(model=target, tier=tier, reason=reason)


def route_with_policy(
    text: str,
    current_model: str,
    config: dict[str, Any] | None = None,
    available_models: list[str] | None = None,
    candidates: list[Candidate] | None = None,
    required_context: int = 0,
) -> RoutingDecision:
    """Routes using the configured policy (`cheapest` / `best_value` / `balanced`).

    `route()` picks the model configured for a tier. When the user asks for a
    policy instead — "the cheapest model that works", "the best quality within
    budget" — the choice happens across the candidate list, so price and context
    window are weighed rather than assumed.
    """
    config = config or {}
    block = config.get("model_router")
    block = block if isinstance(block, dict) else {}
    policy = normalize_router_policy(block.get("policy"))

    if policy == "balanced" or not candidates:
        return route(text, current_model, config, available_models)

    pin = block.get("pin") or config.get("model_pinned")
    if isinstance(pin, str) and pin.strip():
        return RoutingDecision(pin.strip(), "pinned", "pinned in config", pinned=True)

    usable = [c for c in candidates if _model_available(c.model, available_models)]
    max_cost = block.get("max_cost_per_1k")
    max_cost = float(max_cost) if isinstance(max_cost, (int, float)) and max_cost > 0 else None
    chosen, reason = pick_candidate(
        usable, policy, required_context=required_context, max_cost_per_1k=max_cost
    )
    if chosen is None:
        return RoutingDecision(
            current_model,
            "current",
            f"{reason}; keeping the current model",
            fallback=True,
        )
    caps = check_capabilities(
        chosen.model,
        context_window=chosen.context_window,
        cost_per_1k=chosen.cost_per_1k,
        supports_tools=chosen.tools,
        required_context=required_context,
    )
    if not caps.usable:
        return RoutingDecision(
            current_model,
            "current",
            f"`{chosen.model}` is missing: {', '.join(caps.missing)}",
            fallback=True,
        )
    tier, _ = classify_task(text)
    return RoutingDecision(chosen.model, tier, f"{policy}: {reason}")


def apply_live_route(
    text: str,
    current_model: str,
    config: dict[str, Any],
    available_models: list[str] | None = None,
    has_history: bool = True,
    role: str | None = None,
) -> RoutingDecision:
    """Like `route`, but respects `never_switch_mid_task`.

    Blueprint rule: the model must not change silently in the middle of a task.
    When the config opts in (default), a non-trivial session that already has
    messages keeps its model.
    """
    block = config.get("model_router")
    block = block if isinstance(block, dict) else {}
    keep = block.get("never_switch_mid_task")
    if keep is None:
        keep = True
    decision = route(text, current_model, config, available_models, role=role)
    if keep and has_history and not decision.pinned and decision.model != current_model:
        return RoutingDecision(
            model=current_model,
            tier="current",
            reason=f"{decision.reason}; model kept mid-task (never_switch_mid_task)",
            fallback=True,
        )
    return decision


def tier_of(config: dict[str, Any] | None, model: str) -> str:
    """Reverse lookup: which tier a model is configured for."""
    for tier, mapped in _tier_map(config).items():
        if mapped == model:
            return tier
    return "unknown"


def normalize_tier(raw: Any) -> str | None:
    """Coerces aliases (`best`, `cheap`, ...) into a canonical tier id."""
    if not isinstance(raw, str):
        return None
    key = raw.strip().lower()
    if key in TIERS:
        return key
    if key in DEEP_ALIASES:
        return "deep"
    if key in FAST_ALIASES:
        return "fast"
    if key in {"balanced", "auto", "default", "normal"}:
        return "balanced"
    return None
