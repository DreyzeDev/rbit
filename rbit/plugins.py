"""Plugin hooks.

Two extension styles are supported:

* Entry points in the ``rbit.plugins`` group (``setup(app)``) - kept for
  backwards compatibility.
* Local plugin modules listed under ``"plugins"`` in ``.rbit/config.json``
  (paths or importable names).  A module can expose:
    - ``register(registry)``, and/or
    - ``HOOKS = {"tool.execute.before": fn, ...}``, and/or
    - functions named ``on_tool_before``, ``on_tool_after``, ``on_chat_params``,
      ``on_event``.

Hook names follow opencode: ``tool.execute.before``, ``tool.execute.after``,
``chat.params``, ``event``.  Before/after hooks receive a mutable context dict;
raise or set ``ctx["deny"]`` to block a tool.
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

logger = logging.getLogger("rbit.plugins")

HookFn = Callable[[dict[str, Any]], Any | Awaitable[Any]]

HOOK_ALIASES = {
    "on_tool_before": "tool.execute.before",
    "on_tool_after": "tool.execute.after",
    "on_before_edit": "before_edit",
    "on_after_edit": "after_edit",
    "on_before_command": "before_command",
    "on_after_command": "after_command",
    "on_error": "on_error",
    "on_stop": "on_stop",
    "on_session_start": "session_start",
    "on_chat_params": "chat.params",
    "on_chat_message": "chat.message",
    "on_event": "event",
}


class PluginRegistry:
    def __init__(self, policy: Any = None) -> None:
        self._hooks: dict[str, list[tuple[str, HookFn]]] = {}
        #: Limits and approval state; see `rbit.policy.HookPolicy`.
        self.policy = policy

    def on(self, hook: str, callback: HookFn, *, name: str = "") -> None:
        self._hooks.setdefault(hook, []).append((name or getattr(callback, "__name__", "hook"), callback))

    def load_object(self, obj: Any, *, name: str = "") -> None:
        register = getattr(obj, "register", None)
        if callable(register):
            register(self)
        hooks = getattr(obj, "HOOKS", None)
        if isinstance(hooks, dict):
            for hook, callback in hooks.items():
                if callable(callback):
                    self.on(str(hook), callback, name=name)
        for attr, hook in HOOK_ALIASES.items():
            callback = getattr(obj, attr, None)
            if callable(callback):
                self.on(hook, callback, name=name)

    def count(self, hook: str) -> int:
        return len(self._hooks.get(hook, []))

    def hook_names(self) -> list[str]:
        """Every registered hook callback name, for the approval prompt."""
        return sorted({name for entries in self._hooks.values() for name, _cb in entries})

    async def trigger(self, hook: str, context: dict[str, Any]) -> dict[str, Any]:
        """Runs a hook chain under the policy's limits.

        Hooks are third-party code executed on every tool call, so each one is
        bounded by a timeout, its output is capped, and an unapproved hook never
        runs at all — otherwise opening a cloned repo would silently execute it.
        """
        from .policy import HookPolicy, run_hook_safely

        policy = self.policy if isinstance(self.policy, HookPolicy) else HookPolicy(require_approval=False)

        def _note_failure(name: str, reason: str) -> None:
            logger.debug("plugin hook %s (%s) failed: %s", hook, name, reason)

        for name, callback in list(self._hooks.get(hook, [])):
            context = await run_hook_safely(name, callback, context, policy, _note_failure)
        return context

    def trigger_soon(self, hook: str, context: dict[str, Any]) -> None:
        """Fire-and-forget trigger for sync call sites (schedules on the loop)."""
        import asyncio

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self.trigger(hook, context))


def _load_module(name: str, app: Any) -> Any:
    if name.endswith(".py") or "/" in name or "\\" in name:
        path = Path(name).expanduser()
        if not path.is_absolute():
            workspace = Path(getattr(app, "workspace", Path.cwd()))
            path = workspace / path
        if path.is_dir():
            for candidate in (path / "__init__.py", path / "plugin.py", path / "main.py"):
                if candidate.exists():
                    path = candidate
                    break
        module_name = f"rbit_plugin_{path.stem}_{abs(hash(str(path.resolve()))) % 100000}"
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load plugin file: {path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        return module
    return importlib.import_module(name)


def load_plugins(app: Any, config: dict[str, Any] | None = None, registry: PluginRegistry | None = None) -> list[str]:
    """Loads entry points and config `plugins`; returns plugin names."""
    from .policy import HookPolicy

    loaded: list[str] = []
    if registry is None:
        registry = PluginRegistry(policy=HookPolicy.from_config(config))
    elif registry.policy is None:
        registry.policy = HookPolicy.from_config(config)
    try:
        from importlib.metadata import entry_points

        eps = entry_points(group="rbit.plugins")
    except Exception:  # noqa: BLE001 - metadata backends vary across Python versions
        eps = []
    for ep in eps:
        try:
            obj = ep.load()
            obj(app)
            registry.load_object(obj, name=ep.name)
            loaded.append(ep.name)
            logger.info("plugin loaded: %s", ep.name)
        except Exception:
            logger.debug("plugin %s failed", getattr(ep, "name", "?"), exc_info=True)
    entries = (config or {}).get("plugins")
    if isinstance(entries, list):
        for entry in entries:
            if not isinstance(entry, str) or not entry.strip():
                continue
            name = entry.strip()
            try:
                module = _load_module(name, app)
            except Exception:
                logger.warning("local plugin %s failed to import", name, exc_info=True)
                continue
            registry.load_object(module, name=name)
            setup = getattr(module, "setup", None)
            if callable(setup):
                try:
                    setup(app)
                except Exception:
                    logger.warning("local plugin %s setup failed", name, exc_info=True)
                    continue
            loaded.append(name)
            logger.info("local plugin loaded: %s", name)
    return loaded
