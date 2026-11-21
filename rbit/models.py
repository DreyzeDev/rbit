"""Core data models for rbit sessions, messages, tool calls, todos, snapshots, and checkpoints."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any
import uuid

try:
    import tiktoken
except ImportError:  # optional dependency
    tiktoken = None

_ENCODER: Any = None


def count_tokens(text: str) -> int:
    """Accurate token count via tiktoken when installed; ~4 chars/token fallback."""
    if not text:
        return 0
    global _ENCODER
    if tiktoken is not None:
        try:
            if _ENCODER is None:
                _ENCODER = tiktoken.get_encoding("cl100k_base")
            return len(_ENCODER.encode(text))
        except Exception:
            pass
    return max(1, len(text) // 4)


def now_iso() -> str:
    return datetime.now().strftime("%H:%M")


def short_id() -> str:
    return uuid.uuid4().hex[:8]


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


@dataclass
class TodoItem:
    id: str
    content: str
    status: str = "pending"   # pending | in_progress | completed | cancelled
    priority: str = "medium"  # high | medium | low

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TodoItem":
        return cls(
            id=data.get("id") or short_id(),
            content=data.get("content", ""),
            status=data.get("status", "pending"),
            priority=data.get("priority", "medium"),
        )


@dataclass
class ToolCallRecord:
    id: str
    name: str
    arguments: dict[str, Any]
    status: str = "running"   # running | success | error | denied
    summary: str = ""
    output: str = ""
    diff: str = ""
    duration_ms: int = 0
    pid: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ToolCallRecord":
        data = data if isinstance(data, dict) else {}
        args = data.get("arguments")
        return cls(
            id=str(data.get("id") or short_id()),
            name=str(data.get("name") or ""),
            arguments=args if isinstance(args, dict) else {},
            status=str(data.get("status") or "success"),
            summary=str(data.get("summary") or ""),
            output=str(data.get("output") or ""),
            diff=str(data.get("diff") or ""),
            duration_ms=_as_int(data.get("duration_ms")),
            pid=_as_int(data.get("pid")),
        )


@dataclass
class ChatMessage:
    id: str
    role: str                 # user | assistant | system
    content: str
    agent: str = "build"      # build | plan
    model: str = ""
    timestamp: str = field(default_factory=now_iso)
    tokens: int = 0
    duration_s: float = 0.0
    interrupted: bool = False
    attachments: list[str] = field(default_factory=list)
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    pinned: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ChatMessage":
        tcs = [
            ToolCallRecord.from_dict(tc)
            for tc in (data.get("tool_calls") or [])
            if isinstance(tc, dict)
        ]
        return cls(
            id=data.get("id") or short_id(),
            role=data.get("role", "user"),
            content=data.get("content", ""),
            agent=data.get("agent", "build"),
            model=data.get("model", ""),
            timestamp=data.get("timestamp", now_iso()),
            tokens=_as_int(data.get("tokens")),
            duration_s=float(data.get("duration_s") or 0.0),
            interrupted=bool(data.get("interrupted", False)),
            attachments=list(data.get("attachments") or []),
            tool_calls=tcs,
            pinned=bool(data.get("pinned", False)),
        )


@dataclass
class FileChangeStat:
    path: str
    added: str | int = 0
    removed: str | int = 0

    def __post_init__(self) -> None:
        self.added = int(self.added)
        self.removed = int(self.removed)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "FileChangeStat":
        data = data if isinstance(data, dict) else {}
        return cls(
            path=str(data.get("path") or ""),
            added=_as_int(data.get("added")),
            removed=_as_int(data.get("removed")),
        )


@dataclass
class FileSnapshot:
    """Single-file snapshot for /undo and /redo."""
    step_label: str
    path: str
    existed_before: bool
    previous_content: str
    new_content: str


@dataclass
class CheckpointMeta:
    """Workspace-wide named checkpoint metadata (works even without Git)."""
    id: str
    label: str
    created_at: str
    file_count: int


SESSION_SCHEMA_VERSION = 1


def _migrate_session_dict(data: dict[str, Any]) -> dict[str, Any]:
    """Brings older session payloads up to SESSION_SCHEMA_VERSION."""
    version = int(data.get("schema_version") or 0)
    if version < 1:
        # v0 -> v1: pinned flag, per-message ids, sanitized timestamps.
        for m in data.get("messages", []):
            if isinstance(m, dict):
                m.setdefault("pinned", False)
                m.setdefault("id", short_id())
    data["schema_version"] = SESSION_SCHEMA_VERSION
    return data


@dataclass
class Session:
    id: str = field(default_factory=short_id)
    title: str = "Новая сессия"
    created_at: str = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M"))
    messages: list[ChatMessage] = field(default_factory=list)
    todos: list[TodoItem] = field(default_factory=list)
    modified_files: dict[str, FileChangeStat] = field(default_factory=dict)
    finished: bool = True
    archived: bool = False

    @property
    def total_tokens(self) -> int:
        # `m.tokens` already covers the turn's tool output, so tool output is only
        # counted separately for messages that carry no token total of their own.
        total = 0
        for m in self.messages:
            if m.tokens:
                total += m.tokens
            else:
                total += count_tokens(m.content) + sum(
                    count_tokens(tc.output) for tc in m.tool_calls
                )
        return total

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SESSION_SCHEMA_VERSION,
            "id": self.id,
            "title": self.title,
            "created_at": self.created_at,
            "messages": [m.to_dict() for m in self.messages],
            "todos": [t.to_dict() for t in self.todos],
            "modified_files": {k: v.to_dict() for k, v in self.modified_files.items()},
            "finished": self.finished,
            "archived": self.archived,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Session":
        data = _migrate_session_dict(data)
        return cls(
            id=data.get("id") or short_id(),
            title=data.get("title", "Session"),
            created_at=data.get("created_at", ""),
            messages=[ChatMessage.from_dict(m) for m in (data.get("messages") or [])],
            todos=[TodoItem.from_dict(t) for t in (data.get("todos") or [])],
            modified_files={
                k: FileChangeStat.from_dict(v)
                for k, v in (data.get("modified_files") or {}).items()
            },
            finished=data.get("finished", True),
            archived=bool(data.get("archived", False)),
        )


@dataclass
class ModelInfo:
    id: str
    name: str
    provider: str
    context_window: int = 200_000
    cost_per_1k: float = 0.003
    is_local_smart: bool = False
    #: False when the price is unknown, so no cost may be invented for the model.
    priced: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "provider": self.provider,
            "context_window": self.context_window,
            "cost_per_1k": self.cost_per_1k,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ModelInfo":
        return cls(
            id=str(data.get("id", "")),
            name=str(data.get("name") or data.get("id", "")),
            provider=str(data.get("provider") or "Custom"),
            context_window=int(data.get("context_window") or 200_000),
            cost_per_1k=float(data.get("cost_per_1k") or 0.0),
        )


AVAILABLE_MODELS: list[ModelInfo] = [
    ModelInfo(
        id="rbit/smart-local",
        name="rbit Smart Agent",
        provider="rbit Built-in",
        context_window=128_000,
        cost_per_1k=0.0,
        is_local_smart=True,
    ),
    ModelInfo(
        id="anthropic/claude-sonnet-4.5",
        name="Claude Sonnet 4.5",
        provider="Anthropic",
        context_window=200_000,
        cost_per_1k=0.003,
    ),
    ModelInfo(
        id="coarena/gemini-4-carbon",
        name="Gemini 4 Carbon",
        provider="Google",
        context_window=1_000_000,
        cost_per_1k=0.002,
    ),
    ModelInfo(
        id="openai/gpt-4.1",
        name="GPT-4.1",
        provider="OpenAI",
        context_window=128_000,
        cost_per_1k=0.002,
    ),
    ModelInfo(
        id="deepseek/deepseek-chat-v3",
        name="DeepSeek V3",
        provider="DeepSeek",
        context_window=128_000,
        cost_per_1k=0.0005,
    ),
    ModelInfo(
        id="qwen2.5-coder:7b",
        name="Qwen 2.5 Coder 7B",
        provider="Ollama",
        context_window=32_768,
        cost_per_1k=0.0,
    ),
]


def get_model_info(model_id: str, extra_models: list[ModelInfo] | None = None) -> ModelInfo:
    for m in AVAILABLE_MODELS:
        if m.id == model_id:
            return m
    if extra_models:
        for m in extra_models:
            if m.id == model_id:
                return m
    if model_id.startswith("ollama/"):
        clean = model_id.split("/", 1)[1]
        return ModelInfo(id=model_id, name=f"{clean} (Local)", provider="Ollama", context_window=32_768, cost_per_1k=0.0)
    # Unknown model: `priced=False` — the usage ledger must not book a made-up
    # price for it (the default cost_per_1k stays only as a conservative guard
    # estimate for per-turn spending caps).
    return ModelInfo(id=model_id, name=model_id, provider="Custom", priced=False)

def search_session(
    session: Session, query: str, limit: int = 100
) -> list[tuple[str, str, str]]:
    """Case-insensitive search over message text and tool output.

    Returns `(message_id, label, snippet)` triples in conversation order.
    """
    q = query.strip().lower()
    if not q:
        return []
    hits: list[tuple[str, str, str]] = []
    for m in session.messages:
        if len(hits) >= limit:
            break
        body = m.content or ""
        lowered = body.lower()
        if q in lowered:
            pos = lowered.find(q)
            start = max(0, pos - 40)
            snippet = " ".join(body[start : pos + len(q) + 60].split())
            label = f"User ({m.timestamp})" if m.role == "user" else f"rbit ({m.timestamp})"
            hits.append((m.id, label, snippet))
        for tc in m.tool_calls:
            if len(hits) >= limit:
                break
            haystack = f"{tc.name} {tc.summary} {tc.output} {tc.diff}"
            pos = haystack.lower().find(q)
            if pos < 0:
                continue
            start = max(0, pos - 40)
            snippet = " ".join(haystack[start : pos + len(q) + 60].split())
            hits.append((m.id, f"Tool {tc.name}", snippet))
    return hits
