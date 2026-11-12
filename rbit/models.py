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

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ToolCallRecord":
        return cls(**data)


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
    attachments: list[str] = field(default_factory=list)
    tool_calls: list[ToolCallRecord] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ChatMessage":
        tcs = [ToolCallRecord.from_dict(tc) for tc in data.get("tool_calls", [])]
        return cls(
            id=data.get("id") or short_id(),
            role=data.get("role", "user"),
            content=data.get("content", ""),
            agent=data.get("agent", "build"),
            model=data.get("model", ""),
            timestamp=data.get("timestamp", now_iso()),
            tokens=data.get("tokens", 0),
            duration_s=data.get("duration_s", 0.0),
            attachments=data.get("attachments", []),
            tool_calls=tcs,
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
        return cls(**data)


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


@dataclass
class Session:
    id: str = field(default_factory=short_id)
    title: str = "New Session"
    created_at: str = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M"))
    messages: list[ChatMessage] = field(default_factory=list)
    todos: list[TodoItem] = field(default_factory=list)
    modified_files: dict[str, FileChangeStat] = field(default_factory=dict)

    @property
    def total_tokens(self) -> int:
        base = sum(m.tokens or count_tokens(m.content) for m in self.messages)
        tool_toks = sum(
            count_tokens(tc.output)
            for m in self.messages
            for tc in m.tool_calls
        )
        return base + tool_toks

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "created_at": self.created_at,
            "messages": [m.to_dict() for m in self.messages],
            "todos": [t.to_dict() for t in self.todos],
            "modified_files": {k: v.to_dict() for k, v in self.modified_files.items()},
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Session":
        return cls(
            id=data.get("id") or short_id(),
            title=data.get("title", "Session"),
            created_at=data.get("created_at", ""),
            messages=[ChatMessage.from_dict(m) for m in data.get("messages", [])],
            todos=[TodoItem.from_dict(t) for t in data.get("todos", [])],
            modified_files={
                k: FileChangeStat.from_dict(v)
                for k, v in data.get("modified_files", {}).items()
            },
        )


@dataclass
class ModelInfo:
    id: str
    name: str
    provider: str
    context_window: int = 200_000
    cost_per_1k: float = 0.003
    is_local_smart: bool = False


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
    return ModelInfo(id=model_id, name=model_id, provider="Custom")
