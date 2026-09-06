"""Three-tier JSON memory for the research workflow agent."""

from __future__ import annotations

import json
import re
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _read(path: Path, default: Any) -> Any:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def _write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _tokens(text: str) -> set[str]:
    tokens: set[str] = set()
    for part in re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]+", text.lower()):
        tokens.add(part)
        if re.fullmatch(r"[\u4e00-\u9fff]+", part):
            tokens.update(part)
            tokens.update(part[index:index + 2] for index in range(len(part) - 1))
    return tokens


@dataclass
class WorkingMemory:
    task_id: str
    plan: list[str] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    progress: dict[str, str] = field(default_factory=dict)
    updated_at: str = field(default_factory=_now)


@dataclass
class ShortTermSession:
    session_id: str
    messages: list[dict[str, str]] = field(default_factory=list)
    summary: str = ""
    updated_at: str = field(default_factory=_now)


@dataclass
class LongTermMemory:
    id: str
    category: str
    content: str
    source: str
    confidence: float
    confirmed: bool
    created_at: str
    updated_at: str
    supersedes: list[str] = field(default_factory=list)


class MemoryStore:
    """Working, short-term and long-term memory with explicit trust gates."""

    TRUSTED_SOURCES = {"user", "reviewer", "lead", "migration"}
    DURABLE_CATEGORIES = {
        "preference", "fact", "confirmed_fact", "conclusion",
        "confirmed_conclusion", "research_direction",
    }

    def __init__(self, data_dir: Path, max_long_term: int = 200,
                 max_session_messages: int = 40):
        self.working_path = data_dir / "working_memory.json"
        self.short_term_path = data_dir / "short_term_memory.json"
        self.long_term_path = data_dir / "long_term_memory.json"
        self.legacy_path = data_dir / "memory.json"
        self.max_long_term = max_long_term
        self.max_session_messages = max_session_messages
        self._lock = threading.RLock()
        self._migrate_legacy()

    def _migrate_legacy(self) -> None:
        if self.long_term_path.exists() or not self.legacy_path.exists():
            return
        migrated = []
        for item in _read(self.legacy_path, []):
            created = item.get("created_at", _now())
            migrated.append(asdict(LongTermMemory(
                item.get("id", _id("mem")), item.get("category", "fact"),
                item.get("content", ""), "migration", 1.0, True, created, created,
            )))
        _write(self.long_term_path, migrated)

    # Working memory: current plan, evidence and execution progress.
    def save_working(self, task_id: str, plan: list[str] | None = None,
                     evidence: list[dict[str, Any]] | None = None,
                     progress: dict[str, str] | None = None) -> WorkingMemory:
        memories = _read(self.working_path, {})
        current = memories.get(task_id, {})
        state = WorkingMemory(
            task_id, list(plan if plan is not None else current.get("plan", [])),
            list(evidence if evidence is not None else current.get("evidence", [])),
            dict(progress if progress is not None else current.get("progress", {})),
        )
        memories[task_id] = asdict(state)
        _write(self.working_path, memories)
        return state

    def load_working(self, task_id: str) -> WorkingMemory | None:
        item = _read(self.working_path, {}).get(task_id)
        return WorkingMemory(**item) if item else None

    def add_evidence(self, task_id: str, content: str, source: str,
                     confidence: float = 0.5) -> WorkingMemory:
        state = self.load_working(task_id) or WorkingMemory(task_id)
        state.evidence.append({"content": content, "source": source,
                               "confidence": self._confidence(confidence),
                               "created_at": _now()})
        return self.save_working(task_id, state.plan, state.evidence, state.progress)

    def update_progress(self, task_id: str, step: str, status: str) -> WorkingMemory:
        state = self.load_working(task_id) or WorkingMemory(task_id)
        state.progress[step] = status
        return self.save_working(task_id, state.plan, state.evidence, state.progress)

    def clear_working(self, task_id: str) -> None:
        memories = _read(self.working_path, {})
        memories.pop(task_id, None)
        _write(self.working_path, memories)

    # Short-term memory: persistent turns plus bounded compression.
    def append_turn(self, session_id: str, role: str, content: str) -> ShortTermSession:
        with self._lock:
            sessions = _read(self.short_term_path, {})
            session = ShortTermSession(**sessions.get(session_id, {"session_id": session_id}))
            session.messages.append({"role": role, "content": content, "created_at": _now()})
            if len(session.messages) > self.max_session_messages:
                removed = session.messages[:-self.max_session_messages]
                session.messages = session.messages[-self.max_session_messages:]
                session.summary = self._merge_summary(session.summary, removed)
            session.updated_at = _now()
            sessions[session_id] = asdict(session)
            _write(self.short_term_path, sessions)
            return session

    def load_session(self, session_id: str) -> ShortTermSession:
        item = _read(self.short_term_path, {}).get(session_id)
        return ShortTermSession(**item) if item else ShortTermSession(session_id)

    def forget_session(self, session_id: str) -> None:
        sessions = _read(self.short_term_path, {})
        sessions.pop(session_id, None)
        _write(self.short_term_path, sessions)

    def compress_messages(self, messages: list[dict], session_id: str,
                          max_messages: int = 12, keep_recent: int = 6) -> list[dict]:
        if len(messages) <= max_messages:
            return messages
        with self._lock:
            summary = self._merge_summary(self.load_session(session_id).summary,
                                          messages[:-keep_recent])
            sessions = _read(self.short_term_path, {})
            session = self.load_session(session_id)
            session.summary, session.updated_at = summary, _now()
            sessions[session_id] = asdict(session)
            _write(self.short_term_path, sessions)
        return [{"role": "user", "content": f"[Earlier conversation summary]\n{summary}"},
                *messages[-keep_recent:]]

    @staticmethod
    def _merge_summary(existing: str, messages: list[dict]) -> str:
        parts = [existing] if existing else []
        for message in messages:
            content = message.get("content", "")
            if not isinstance(content, str):
                content = json.dumps(content, ensure_ascii=False, default=str)
            parts.append(f"{message.get('role', 'unknown')}: {content[:300]}")
        return "\n".join(parts)[-4000:]

    # Long-term memory: only stable, confirmed knowledge is admitted.
    def remember(self, content: str, category: str = "fact", source: str = "user",
                 confidence: float = 1.0, confirmed: bool = True,
                 reviewed_by: str | None = None, stable: bool = True) -> str:
        if not stable or not confirmed or (source not in self.TRUSTED_SOURCES and not reviewed_by):
            raise ValueError("Long-term memory requires confirmation from user, Lead, or Reviewer")
        now = _now()
        item = LongTermMemory(_id("mem"), category, content, source,
                              self._confidence(confidence), True, now, now)
        items = _read(self.long_term_path, [])
        items.append(asdict(item))
        _write(self.long_term_path, self._evict(items))
        return item.id

    def recall(self, query: str = "", limit: int = 10) -> list[dict]:
        items = [item for item in _read(self.long_term_path, []) if item.get("confirmed")]
        if query:
            words = _tokens(query)
            ranked = [(len(words & _tokens(item["content"])), item) for item in items]
            return [item for score, item in sorted(ranked, key=lambda x: x[0], reverse=True)
                    if score][:limit]
        return items[-limit:]

    def update_memory(self, memory_id: str, content: str, source: str = "user",
                      confidence: float = 1.0, reviewed_by: str | None = None) -> str:
        items = _read(self.long_term_path, [])
        old = next((item for item in items if item["id"] == memory_id), None)
        if not old:
            raise KeyError(memory_id)
        new_id = self.remember(content, old["category"], source, confidence,
                               reviewed_by=reviewed_by)
        items = _read(self.long_term_path, [])
        next(item for item in items if item["id"] == new_id)["supersedes"] = [memory_id]
        _write(self.long_term_path, [item for item in items if item["id"] != memory_id])
        return new_id

    def forget(self, memory_id: str) -> bool:
        items = _read(self.long_term_path, [])
        kept = [item for item in items if item["id"] != memory_id]
        _write(self.long_term_path, kept)
        return len(kept) != len(items)

    def detect_conflicts(self, content: str, category: str) -> list[dict]:
        words = _tokens(content)
        return [item for item in _read(self.long_term_path, [])
                if item["category"] == category and words & _tokens(item["content"])
                and item["content"].strip().lower() != content.strip().lower()]

    def merge(self, memory_ids: list[str], content: str, source: str = "reviewer",
              confidence: float = 1.0) -> str:
        items = _read(self.long_term_path, [])
        selected = [item for item in items if item["id"] in memory_ids]
        if len(selected) != len(set(memory_ids)):
            raise KeyError("One or more memories do not exist")
        categories = {item["category"] for item in selected}
        if len(categories) != 1:
            raise ValueError("Only memories in the same category can be merged")
        new_id = self.remember(content, categories.pop(), source, confidence,
                               reviewed_by=source)
        items = _read(self.long_term_path, [])
        next(item for item in items if item["id"] == new_id)["supersedes"] = list(memory_ids)
        _write(self.long_term_path, [item for item in items if item["id"] not in memory_ids])
        return new_id

    def build_context(self, query: str, session_id: str, limit: int = 5) -> str:
        session = self.load_session(session_id)
        return json.dumps({"conversation_summary": session.summary,
                           "recalled_long_term_memory": self.recall(query, limit)},
                          ensure_ascii=False)

    def _evict(self, items: list[dict]) -> list[dict]:
        if len(items) <= self.max_long_term:
            return items
        return sorted(items, key=lambda item: (float(item.get("confidence", 0)),
                                                item.get("updated_at", "")))[-self.max_long_term:]

    @staticmethod
    def _confidence(value: float) -> float:
        value = float(value)
        if not 0.0 <= value <= 1.0:
            raise ValueError("confidence must be between 0 and 1")
        return value
