"""Small, observable fallback layer for models and external research services."""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


def operation_id(service: str, payload: Any) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return f"{service}_{hashlib.sha256(encoded).hexdigest()[:20]}"


def classify_complexity(text: str) -> str:
    """A deterministic routing hint; it never delegates authorization to a model."""
    complex_markers = ("比较", "综合", "评审", "架构", "多篇", "experiment",
                       "compare", "synthesize", "review", "architecture")
    return "strong" if len(text) > 180 or any(marker in text.lower() for marker in complex_markers) else "light"


@dataclass(frozen=True)
class ModelEndpoint:
    name: str
    client: Any
    model: str
    tier: str = "strong"
    primary: bool = False


class FallbackJournal:
    """Append-only switch journal plus idempotent results and deferred operations."""

    def __init__(self, data_dir: Path):
        self.events_path = data_dir / "fallback_events.jsonl"
        self.cache_path = data_dir / "fallback_results.json"
        self.pending_path = data_dir / "pending_external_tasks.json"
        self._lock = threading.RLock()

    def execute(self, service: str, op_id: str, providers: list[tuple[str, Callable]],
                cache_result: bool = False) -> Any:
        with self._lock:
            cached = self._read(self.cache_path, {}).get(op_id)
        if cached is not None:
            self.log(op_id, service, "cache", "cache", "reused", "idempotent replay")
            return cached
        errors = []
        for index, (name, operation) in enumerate(providers):
            try:
                result = operation()
                previous = providers[index - 1][0] if index else name
                self.log(op_id, service, previous, name, "succeeded",
                         "primary" if index == 0 else "fallback")
                if cache_result:
                    with self._lock:
                        cache = self._read(self.cache_path, {})
                        cache[op_id] = result
                        self._write(self.cache_path, cache)
                return result
            except Exception as exc:
                errors.append(f"{name}: {exc}")
                target = providers[index + 1][0] if index + 1 < len(providers) else "pending"
                self.log(op_id, service, name, target, "failed", str(exc))
        raise RuntimeError("; ".join(errors))

    def defer(self, service: str, payload: dict[str, Any], reason: str) -> str:
        op_id = operation_id(service, payload)
        with self._lock:
            pending = self._read(self.pending_path, {})
            if op_id not in pending:
                pending[op_id] = {"id": op_id, "service": service, "payload": payload,
                                  "reason": reason, "status": "pending", "created_at": self._now()}
                self._write(self.pending_path, pending)
        self.log(op_id, service, "all", "pending", "deferred", reason)
        return op_id

    def log(self, op_id: str, service: str, source: str, target: str,
            status: str, reason: str) -> None:
        record = {"time": self._now(), "operation_id": op_id, "service": service,
                  "from": source, "to": target, "status": status, "reason": reason}
        with self._lock:
            self.events_path.parent.mkdir(parents=True, exist_ok=True)
            with self.events_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    @staticmethod
    def _read(path: Path, default: Any) -> Any:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default

    @staticmethod
    def _write(path: Path, value: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)


class ResilientModelClient:
    """Anthropic-compatible client that routes by complexity and fails over."""

    def __init__(self, endpoints: list[ModelEndpoint], journal: FallbackJournal):
        if not endpoints:
            raise ValueError("At least one model endpoint is required")
        self.endpoints, self.journal = endpoints, journal
        self.messages = self

    def _ordered(self, complexity: str) -> list[ModelEndpoint]:
        matching = [item for item in self.endpoints if item.tier == complexity]
        remaining = [item for item in self.endpoints if item not in matching]
        return sorted(matching, key=lambda item: not item.primary) + sorted(
            remaining, key=lambda item: not item.primary)

    def create_for_task(self, complexity: str, **kwargs):
        endpoints = self._ordered(complexity)
        op_id = operation_id("model", {"complexity": complexity,
                                        "messages": kwargs.get("messages", [])})
        providers = []
        for endpoint in endpoints:
            request = dict(kwargs)
            request["model"] = endpoint.model
            providers.append((endpoint.name,
                              lambda endpoint=endpoint, request=request:
                              endpoint.client.messages.create(**request)))
        return self.journal.execute("model", op_id, providers)

    def create(self, **kwargs):
        return self.create_for_task("strong", **kwargs)


class FallbackEmbeddingModel:
    """Use a local embedding when the configured remote embedding fails."""

    def __init__(self, primary: Any, fallback: Any, journal: FallbackJournal):
        self.primary, self.fallback, self.journal = primary, fallback, journal
        self.dimensions = getattr(primary, "dimensions", getattr(fallback, "dimensions", 0))

    def embed_many(self, texts: list[str]) -> list[list[float]]:
        op_id = operation_id("embedding", texts)
        return self.journal.execute("embedding", op_id, [
            ("remote", lambda: self.primary.embed_many(texts)),
            ("local", lambda: self.fallback.embed_many(texts)),
        ])

    def embed(self, text: str) -> list[float]:
        return self.embed_many([text])[0]


class FallbackKnowledgeBase:
    """Mirror writes locally and use the local index when Elasticsearch fails."""

    def __init__(self, primary: Any, fallback: Any, journal: FallbackJournal):
        self.primary, self.fallback, self.journal = primary, fallback, journal

    def add_document(self, title: str, content: str, source: str = "manual",
                     chunk_size: int = 800) -> str:
        payload = {"title": title, "content": content, "source": source,
                   "chunk_size": chunk_size}
        op_id = operation_id("knowledge_write", payload)

        def remote_with_mirror():
            result = self.primary.add_document(**payload)
            self.fallback.add_document(**payload)
            return result

        return self.journal.execute("knowledge", op_id, [
            ("elasticsearch", remote_with_mirror),
            ("local", lambda: self.fallback.add_document(**payload)),
        ], cache_result=True)

    def search_knowledge(self, query: str, limit: int = 5) -> list[dict]:
        op_id = operation_id("knowledge_search", {"query": query, "limit": limit})
        return self.journal.execute("knowledge", op_id, [
            ("elasticsearch", lambda: self.primary.search_knowledge(query, limit)),
            ("local", lambda: self.fallback.search_knowledge(query, limit)),
        ])
