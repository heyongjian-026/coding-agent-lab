"""A small, teachable research workflow agent harness."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import re
import subprocess
import threading
import time
import base64
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from bad_cases import BadCaseStore
from memory_system import LongTermMemory, MemoryStore, ShortTermSession, WorkingMemory
from multi_agent import (AgentTeam, MessageBus, ROLE_SPECS, WorkflowState,
                         parse_role_result)
from resilience import (FallbackEmbeddingModel, FallbackJournal,
                        FallbackKnowledgeBase, ModelEndpoint,
                        ResilientModelClient, classify_complexity, operation_id)
from observability import InstrumentedEmbedding, TraceStore


DATA_DIR_NAME = ".research-agent"
MODEL = os.getenv("MODEL_ID", "")


def _id(prefix: str) -> str:
    return f"{prefix}_{int(time.time())}_{random.randint(0, 9999):04d}"


def _write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _read_json(path: Path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


@dataclass
class Task:
    id: str
    subject: str
    description: str = ""
    status: str = "pending"
    owner: str | None = None
    blockedBy: list[str] = field(default_factory=list)
    due_at: str | None = None


class TaskStore:
    """Persistent task board, following the s20 Task naming and lifecycle."""

    def __init__(self, data_dir: Path):
        self.tasks_dir = data_dir / "tasks"
        self.tasks_dir.mkdir(parents=True, exist_ok=True)

    def _task_path(self, task_id: str) -> Path:
        if not re.fullmatch(r"task_[A-Za-z0-9_-]+", task_id):
            raise ValueError("Invalid task id")
        return self.tasks_dir / f"{task_id}.json"

    def save_task(self, task: Task) -> None:
        _write_json(self._task_path(task.id), asdict(task))

    def load_task(self, task_id: str) -> Task:
        return Task(**_read_json(self._task_path(task_id), {}))

    def list_tasks(self) -> list[Task]:
        return [Task(**_read_json(path, {})) for path in sorted(self.tasks_dir.glob("task_*.json"))]

    def create_task(self, subject: str, description: str = "",
                    blockedBy: list[str] | None = None, due_at: str | None = None) -> Task:
        task = Task(_id("task"), subject, description, blockedBy=blockedBy or [], due_at=due_at)
        self.save_task(task)
        return task

    def can_start(self, task_id: str) -> bool:
        task = self.load_task(task_id)
        return all(self._task_path(dep).exists() and self.load_task(dep).status == "completed"
                   for dep in task.blockedBy)

    def claim_task(self, task_id: str, owner: str = "agent") -> str:
        task = self.load_task(task_id)
        if task.status != "pending":
            return f"Task {task_id} is {task.status}, cannot claim"
        if not self.can_start(task_id):
            return f"Task {task_id} is blocked"
        task.status, task.owner = "in_progress", owner
        self.save_task(task)
        return f"Claimed {task.id} ({task.subject})"

    def complete_task(self, task_id: str) -> str:
        task = self.load_task(task_id)
        if task.status != "in_progress":
            return f"Task {task_id} is {task.status}, cannot complete"
        task.status = "completed"
        self.save_task(task)
        return f"Completed {task.id} ({task.subject})"


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[\w\u4e00-\u9fff]+", text.lower()))


class LocalEmbeddingModel:
    """Dependency-free hashed token embedding with a replaceable interface."""

    def __init__(self, dimensions: int = 256):
        self.dimensions = dimensions

    def embed(self, text: str) -> list[float]:
        vector = [0.0] * self.dimensions
        for token in _tokens(text):
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            index = int.from_bytes(digest[:4], "big") % self.dimensions
            vector[index] += -1.0 if digest[4] & 1 else 1.0
        norm = math.sqrt(sum(value * value for value in vector))
        return [value / norm for value in vector] if norm else vector

    def embed_many(self, texts: list[str]) -> list[list[float]]:
        return [self.embed(text) for text in texts]


class APIEmbeddingModel:
    """OpenAI-compatible embedding API client, including DashScope."""

    def __init__(self, url: str, api_key: str, model: str,
                 batch_size: int = 10, dimensions: int = 2048, opener=None):
        if not api_key:
            raise ValueError("EMBEDDING_API_KEY is required for API embeddings")
        self.url = url.rstrip("/") + "/embeddings"
        self.api_key = api_key
        self.model = model
        self.batch_size = max(1, min(batch_size, 10))
        self.dimensions = dimensions
        self.opener = opener or urllib.request.urlopen

    def _request(self, texts: list[str]) -> list[list[float]]:
        payload = {"model": self.model, "input": texts, "dimensions": self.dimensions}
        request = urllib.request.Request(
            self.url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Authorization": f"Bearer {self.api_key}",
                     "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with self.opener(request, timeout=60) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Embedding API failed ({exc.code}): {detail}") from exc
        ordered = sorted(body.get("data", []), key=lambda item: item["index"])
        if len(ordered) != len(texts):
            raise RuntimeError("Embedding API returned an unexpected vector count")
        return [item["embedding"] for item in ordered]

    def embed_many(self, texts: list[str]) -> list[list[float]]:
        vectors = []
        for start in range(0, len(texts), self.batch_size):
            vectors.extend(self._request(texts[start:start + self.batch_size]))
        return vectors

    def embed(self, text: str) -> list[float]:
        return self.embed_many([text])[0]


def cosine_similarity(left: list[float], right: list[float]) -> float:
    if len(left) != len(right):
        return 0.0
    return sum(a * b for a, b in zip(left, right))


class KnowledgeBase:
    """Persistent vector RAG with an injectable embedding model."""

    def __init__(self, data_dir: Path, embedding_model=None):
        self.path = data_dir / "knowledge.json"
        self.embedding_model = embedding_model or LocalEmbeddingModel()

    def add_document(self, title: str, content: str, source: str = "manual",
                     chunk_size: int = 800) -> str:
        documents = _read_json(self.path, [])
        doc_id = _id("doc")
        texts = [content[i:i + chunk_size] for i in range(0, len(content), chunk_size)] or [""]
        inputs = [title + " " + text for text in texts]
        embed_many = getattr(self.embedding_model, "embed_many", None)
        embeddings = embed_many(inputs) if embed_many else [self.embedding_model.embed(x) for x in inputs]
        chunks = [{"content": text, "embedding": embedding}
                  for text, embedding in zip(texts, embeddings)]
        documents.append({"id": doc_id, "title": title, "source": source, "chunks": chunks})
        _write_json(self.path, documents)
        return doc_id

    def search_knowledge(self, query: str, limit: int = 5) -> list[dict]:
        query_vector = self.embedding_model.embed(query)
        hits = []
        for doc in _read_json(self.path, []):
            for index, chunk in enumerate(doc["chunks"]):
                # Migrate Stage 1 string chunks when they are first read.
                content = chunk if isinstance(chunk, str) else chunk["content"]
                embedding = (self.embedding_model.embed(doc["title"] + " " + content)
                             if isinstance(chunk, str) else chunk["embedding"])
                score = cosine_similarity(query_vector, embedding)
                if score > 0:
                    hits.append({"document_id": doc["id"], "title": doc["title"],
                                 "source": doc["source"], "chunk": index,
                                 "score": round(score, 6), "content": content})
        return sorted(hits, key=lambda x: (-x["score"], x["title"]))[:limit]


class ElasticsearchKnowledgeBase:
    """Elasticsearch-backed dense-vector RAG store."""

    INDEX_NAME = "research-agent-knowledge"

    def __init__(self, embedding_model, host: str = "localhost", port: int = 9200,
                 scheme: str = "http", username: str = "", password: str = "", opener=None):
        self.embedding_model = embedding_model
        self.base_url = f"{scheme}://{host}:{port}"
        self.username, self.password = username, password
        self.opener = opener or urllib.request.urlopen
        self._ensure_index()

    def _request(self, method: str, path: str, payload: dict | None = None):
        headers = {"Content-Type": "application/json"}
        if self.username:
            token = base64.b64encode(f"{self.username}:{self.password}".encode()).decode()
            headers["Authorization"] = f"Basic {token}"
        request = urllib.request.Request(
            self.base_url + path,
            data=None if payload is None else json.dumps(payload).encode("utf-8"),
            headers=headers,
            method=method,
        )
        try:
            with self.opener(request, timeout=60) as response:
                raw = response.read()
                return json.loads(raw.decode("utf-8")) if raw else {}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Elasticsearch failed ({exc.code}): {detail}") from exc

    def _ensure_index(self) -> None:
        dimensions = self.embedding_model.dimensions
        try:
            self._request("HEAD", f"/{self.INDEX_NAME}")
        except RuntimeError as exc:
            if "(404)" not in str(exc):
                raise
            self._request("PUT", f"/{self.INDEX_NAME}", {"mappings": {"properties": {
                "document_id": {"type": "keyword"}, "title": {"type": "text"},
                "source": {"type": "keyword"}, "chunk": {"type": "integer"},
                "content": {"type": "text"},
                "embedding": {"type": "dense_vector", "dims": dimensions,
                              "index": True, "similarity": "cosine"}}}})

    def add_document(self, title: str, content: str, source: str = "manual",
                     chunk_size: int = 800) -> str:
        doc_id = _id("doc")
        texts = [content[i:i + chunk_size] for i in range(0, len(content), chunk_size)] or [""]
        vectors = self.embedding_model.embed_many([title + " " + text for text in texts])
        for index, (text, vector) in enumerate(zip(texts, vectors)):
            item_id = f"{doc_id}-{index}"
            self._request("PUT", f"/{self.INDEX_NAME}/_doc/{item_id}", {
                "document_id": doc_id, "title": title, "source": source,
                "chunk": index, "content": text, "embedding": vector})
        self._request("POST", f"/{self.INDEX_NAME}/_refresh")
        return doc_id

    def search_knowledge(self, query: str, limit: int = 5) -> list[dict]:
        vector = self.embedding_model.embed(query)
        result = self._request("POST", f"/{self.INDEX_NAME}/_search", {
            "size": limit, "knn": {"field": "embedding", "query_vector": vector,
                                    "k": limit, "num_candidates": max(limit * 10, 100)},
            "_source": ["document_id", "title", "source", "chunk", "content"]})
        return [{**hit["_source"], "score": round(hit.get("_score", 0.0), 6)}
                for hit in result.get("hits", {}).get("hits", [])]


def build_knowledge_base_from_env(data_dir: Path, traces: TraceStore | None = None):
    """Select remote RAG when configured; otherwise retain the local teaching backend."""
    local_embedding = LocalEmbeddingModel()
    if traces:
        local_embedding = InstrumentedEmbedding(local_embedding, traces, "local")
    local = KnowledgeBase(data_dir, local_embedding)
    url = os.getenv("EMBEDDING_API_URL", "").strip()
    model = os.getenv("EMBEDDING_MODEL", "").strip()
    if not (url and model):
        return local
    journal = FallbackJournal(data_dir)
    embedding = APIEmbeddingModel(
        url, os.getenv("EMBEDDING_API_KEY", ""), model,
        int(os.getenv("EMBEDDING_BATCH_SIZE", "10")),
        int(os.getenv("EMBEDDING_DIMENSION", "2048")),
    )
    if traces:
        embedding = InstrumentedEmbedding(embedding, traces, "remote")
    try:
        remote = ElasticsearchKnowledgeBase(
            embedding, os.getenv("ELASTICSEARCH_HOST", "localhost"),
            int(os.getenv("ELASTICSEARCH_PORT", "9200")),
            os.getenv("ELASTICSEARCH_SCHEME", "http"),
            os.getenv("ELASTICSEARCH_USERNAME", "elastic"),
            os.getenv("ELASTICSEARCH_PASSWORD", ""),
        )
    except Exception as exc:
        journal.defer("knowledge_init", {"backend": "elasticsearch"}, str(exc))
        return local
    return FallbackKnowledgeBase(remote, local, journal)


def build_model_client(anthropic_class, data_dir: Path) -> ResilientModelClient:
    """Create one model interface containing primary, fallback and optional tiers."""
    journal = FallbackJournal(data_dir)
    primary_model = os.getenv("MODEL_ID", MODEL)
    primary = anthropic_class(api_key=os.getenv("ANTHROPIC_API_KEY"),
                              base_url=os.getenv("ANTHROPIC_BASE_URL"))
    endpoints = [ModelEndpoint("primary", primary, primary_model,
                               os.getenv("PRIMARY_MODEL_TIER", "strong"), True)]
    fallback_model = os.getenv("FALLBACK_MODEL_ID", "").strip()
    if fallback_model:
        fallback = anthropic_class(
            api_key=os.getenv("FALLBACK_API_KEY") or os.getenv("ANTHROPIC_API_KEY"),
            base_url=os.getenv("FALLBACK_BASE_URL") or os.getenv("ANTHROPIC_BASE_URL"))
        endpoints.append(ModelEndpoint("fallback", fallback, fallback_model,
                                       os.getenv("FALLBACK_MODEL_TIER", "strong")))
    light_model = os.getenv("LIGHT_MODEL_ID", "").strip()
    if light_model and light_model not in {item.model for item in endpoints}:
        endpoints.append(ModelEndpoint("light", primary, light_model, "light"))
    return ResilientModelClient(endpoints, journal)


@dataclass
class ApprovalRequest:
    id: str
    action: str
    payload: dict
    status: str = "pending"


class ApprovalStore:
    def __init__(self, data_dir: Path):
        self.path = data_dir / "approvals.json"

    def request_approval(self, action: str, payload: dict) -> ApprovalRequest:
        items = _read_json(self.path, [])
        request = ApprovalRequest(_id("approval"), action, payload)
        items.append(asdict(request))
        _write_json(self.path, items)
        return request

    def review_approval(self, request_id: str, approve: bool) -> str:
        items = _read_json(self.path, [])
        for item in items:
            if item["id"] == request_id:
                if item["status"] != "pending":
                    return f"Approval {request_id} already {item['status']}"
                item["status"] = "approved" if approve else "rejected"
                _write_json(self.path, items)
                return f"Approval {request_id} {item['status']}"
        return f"Approval {request_id} not found"

    def list_approvals(self) -> list[dict]:
        return _read_json(self.path, [])

    def get_approval(self, request_id: str) -> dict | None:
        for item in self.list_approvals():
            if item["id"] == request_id:
                return item
        return None

    def mark_executed(self, request_id: str) -> None:
        items = self.list_approvals()
        for item in items:
            if item["id"] == request_id:
                item["status"] = "executed"
                _write_json(self.path, items)
                return


@dataclass
class Notification:
    id: str
    channel: str
    recipient: str
    content: str
    approval_id: str
    status: str = "awaiting_approval"
    delivered_at: str | None = None


class NotificationStore:
    """Approval-gated delivery to a local outbox; connectors come in Stage 3."""

    def __init__(self, data_dir: Path, approvals: ApprovalStore):
        self.path = data_dir / "notifications.json"
        self.outbox = data_dir / "notification_outbox.jsonl"
        self.approvals = approvals

    def _items(self) -> list[dict]:
        return _read_json(self.path, [])

    def request_notification(self, channel: str, recipient: str, content: str) -> Notification:
        approval = self.approvals.request_approval(
            "send_notification", {"channel": channel, "recipient": recipient, "content": content})
        notification = Notification(_id("notification"), channel, recipient, content, approval.id)
        items = self._items(); items.append(asdict(notification)); _write_json(self.path, items)
        return notification

    def deliver_notification(self, notification_id: str) -> str:
        items = self._items()
        for item in items:
            if item["id"] != notification_id:
                continue
            approval = self.approvals.get_approval(item["approval_id"])
            if not approval or approval["status"] != "approved":
                return f"Notification {notification_id} blocked: approval required"
            if item["status"] == "delivered":
                return f"Notification {notification_id} already delivered"
            item["status"] = "delivered"
            item["delivered_at"] = datetime.now().isoformat(timespec="seconds")
            _write_json(self.path, items)
            self.outbox.parent.mkdir(parents=True, exist_ok=True)
            with self.outbox.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(item, ensure_ascii=False) + "\n")
            return f"Delivered {notification_id} to local outbox"
        return f"Notification {notification_id} not found"

    def list_notifications(self) -> list[dict]:
        return self._items()


@dataclass
class CronJob:
    id: str
    cron: str
    prompt: str
    recurring: bool = True
    enabled: bool = True


def _cron_field_matches(field: str, value: int) -> bool:
    if field == "*": return True
    if field.startswith("*/"): return value % int(field[2:]) == 0
    if "," in field: return any(_cron_field_matches(part, value) for part in field.split(","))
    if "-" in field:
        left, right = map(int, field.split("-", 1)); return left <= value <= right
    return int(field) == value


def cron_matches(cron_expr: str, dt: datetime) -> bool:
    fields = cron_expr.split()
    if len(fields) != 5: return False
    values = [dt.minute, dt.hour, dt.day, dt.month, (dt.weekday() + 1) % 7]
    return all(_cron_field_matches(field, value) for field, value in zip(fields, values))


def validate_cron(cron_expr: str) -> str | None:
    fields = cron_expr.split()
    if len(fields) != 5: return "Cron must contain 5 fields"
    bounds = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 6)]
    try:
        for field, (low, high) in zip(fields, bounds):
            if field.startswith("*/") and (not field[2:].isdigit() or int(field[2:]) <= 0):
                return f"Invalid cron step: {field}"
            samples = field.replace("*/", "").replace("-", ",").split(",")
            if field != "*" and any(not x.isdigit() or not low <= int(x) <= high for x in samples):
                return f"Invalid cron field: {field}"
    except (ValueError, ZeroDivisionError):
        return f"Invalid cron expression: {cron_expr}"
    return None


class CronScheduler:
    def __init__(self, data_dir: Path):
        self.path = data_dir / "cron_jobs.json"
        self._last_fired: dict[str, str] = {}

    def schedule_job(self, cron: str, prompt: str, recurring: bool = True) -> CronJob:
        error = validate_cron(cron)
        if error: raise ValueError(error)
        jobs = self.list_jobs()
        job = CronJob(_id("cron"), cron, prompt, recurring)
        jobs.append(job)
        _write_json(self.path, [asdict(x) for x in jobs])
        return job

    def list_jobs(self) -> list[CronJob]:
        return [CronJob(**item) for item in _read_json(self.path, [])]

    def due_jobs(self, now: datetime | None = None) -> list[CronJob]:
        now = now or datetime.now(); marker = now.strftime("%Y-%m-%d %H:%M")
        jobs, due = self.list_jobs(), []
        for job in jobs:
            if job.enabled and cron_matches(job.cron, now) and self._last_fired.get(job.id) != marker:
                due.append(job); self._last_fired[job.id] = marker
                if not job.recurring: job.enabled = False
        _write_json(self.path, [asdict(x) for x in jobs])
        return due


def normalize_mcp_name(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "_", name)


class StdioTransport:
    """Minimal persistent JSON-RPC transport for an MCP stdio server."""

    def __init__(self, command: list[str], cwd: Path, env: dict | None = None):
        if not command or not all(isinstance(item, str) and item for item in command):
            raise ValueError("MCP command must be a non-empty argv list")
        process_env = os.environ.copy(); process_env.update(env or {})
        self.process = subprocess.Popen(command, cwd=cwd, env=process_env,
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, text=True, encoding="utf-8")
        self.lock = threading.Lock(); self.request_id = 0

    def request(self, method: str, params: dict | None = None) -> dict:
        with self.lock:
            self.request_id += 1
            payload = {"jsonrpc": "2.0", "id": self.request_id, "method": method,
                       "params": params or {}}
            self.process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            self.process.stdin.flush()
            while True:
                line = self.process.stdout.readline()
                if not line:
                    error = self.process.stderr.read()[-1000:]
                    raise RuntimeError(f"MCP server stopped: {error}")
                message = json.loads(line)
                if message.get("id") != self.request_id:
                    continue
                if "error" in message:
                    raise RuntimeError(str(message["error"]))
                return message.get("result", {})

    def notify(self, method: str, params: dict | None = None) -> None:
        with self.lock:
            payload = {"jsonrpc": "2.0", "method": method, "params": params or {}}
            self.process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            self.process.stdin.flush()

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()


class MCPClient:
    """MCP client that discovers server tools and calls them by name."""

    def __init__(self, name: str, transport):
        self.name = normalize_mcp_name(name)
        self.transport = transport
        self.tools: list[dict] = []

    def connect(self) -> list[dict]:
        self.transport.request("initialize", {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "research-workflow-agent", "version": "0.1.0"},
        })
        if hasattr(self.transport, "notify"):
            self.transport.notify("notifications/initialized")
        result = self.transport.request("tools/list")
        self.tools = result.get("tools", [])
        return self.tools

    def call_tool(self, tool_name: str, args: dict) -> str:
        result = self.transport.request("tools/call", {"name": tool_name, "arguments": args})
        content = result.get("content", [])
        texts = [item.get("text", "") for item in content if item.get("type") == "text"]
        return "\n".join(texts) if texts else json.dumps(result, ensure_ascii=False)


class ResearchRuntime:
    def __init__(self, workspace: Path, embedding_model=None):
        self.workspace = workspace.resolve()
        self.data_dir = self.workspace / DATA_DIR_NAME
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.tasks = TaskStore(self.data_dir)
        self.memory = MemoryStore(self.data_dir)
        self.approvals = ApprovalStore(self.data_dir)
        human_approved = lambda approval_id: bool(
            self.approvals.get_approval(approval_id)
            and self.approvals.get_approval(approval_id)["status"] == "approved")
        self.traces = TraceStore(
            self.data_dir,
            request_human=lambda payload: self.approvals.request_approval(
                "continue_over_budget", payload).id,
            human_approved=human_approved,
        )
        self.bad_cases = BadCaseStore(
            self.data_dir,
            request_human=lambda payload: self.approvals.request_approval(
                "review_bad_case", payload).id,
            human_approved=human_approved,
        )
        selected_embedding = embedding_model
        if selected_embedding:
            selected_embedding = InstrumentedEmbedding(selected_embedding, self.traces, "injected")
        self.knowledge = (KnowledgeBase(self.data_dir, selected_embedding) if selected_embedding
                          else build_knowledge_base_from_env(self.data_dir, self.traces))
        self.notifications = NotificationStore(self.data_dir, self.approvals)
        self.scheduler = CronScheduler(self.data_dir)
        self.fallbacks = FallbackJournal(self.data_dir)
        self.mcp_clients: dict[str, MCPClient] = {}
        self.team: AgentTeam | None = None
        self.tool_lock = threading.RLock()

    def bind_team(self, team: AgentTeam) -> None:
        self.team = team

    def _mcp_config(self) -> dict:
        return _read_json(self.data_dir / "mcp_servers.json", {})

    def connect_mcp(self, name: str, transport=None) -> str:
        safe_name = normalize_mcp_name(name)
        if safe_name in self.mcp_clients:
            return f"MCP server '{safe_name}' already connected"
        if transport is None:
            config = self._mcp_config().get(name)
            if not config:
                return f"MCP server '{name}' is not configured"
            transport = StdioTransport(config.get("command", []), self.workspace,
                                       config.get("env", {}))
        client = MCPClient(safe_name, transport); tools = client.connect()
        self.mcp_clients[safe_name] = client
        return f"Connected to MCP server '{safe_name}'. Discovered {len(tools)} tools"

    def assemble_tool_pool(self, allowed_tool_names: set[str] | None = None) -> list[dict]:
        tools = [tool for tool in TOOLS
                 if allowed_tool_names is None or tool["name"] in allowed_tool_names]
        for server, client in self.mcp_clients.items():
            for definition in client.tools:
                name = f"mcp__{server}__{normalize_mcp_name(definition['name'])}"
                if allowed_tool_names is not None and name not in allowed_tool_names:
                    continue
                tools.append({"name": name, "description": definition.get("description", "MCP tool"),
                              "input_schema": definition.get("inputSchema", {"type": "object"})})
        return tools

    def _mcp_fallback_clients(self, server: str, tool_name: str) -> list[tuple[str, MCPClient, str]]:
        providers = [(server, self.mcp_clients[server], tool_name)]
        config = next((value for name, value in self._mcp_config().items()
                       if normalize_mcp_name(name) == server), {})
        for fallback in config.get("fallback_servers", []):
            safe = normalize_mcp_name(fallback)
            client = self.mcp_clients.get(safe)
            definition = next((tool for tool in client.tools
                               if normalize_mcp_name(tool["name"]) == normalize_mcp_name(tool_name)),
                              None) if client else None
            if client and definition:
                providers.append((safe, client, definition["name"]))
        return providers

    def _call_mcp_with_fallback(self, server: str, tool_name: str, args: dict,
                                mutating: bool = False) -> str:
        payload = {"server": server, "tool": tool_name, "args": args}
        op_id = operation_id("mcp", payload)
        providers = [(name, lambda client=client, actual=actual: client.call_tool(actual, args))
                     for name, client, actual in self._mcp_fallback_clients(server, tool_name)]
        try:
            return self.fallbacks.execute("mcp", op_id, providers, cache_result=mutating)
        except Exception as exc:
            pending_id = self.fallbacks.defer("mcp", payload, str(exc))
            return f"MCP unavailable; operation saved as pending task {pending_id}"

    def resolve_conflict(self, decision_id: str, selected_id: str) -> str:
        if not self.team:
            return "Error: Agent team is not configured"
        decision = self.team.arbitrator.get_decision(decision_id)
        if not decision or not decision.approval_id:
            return "Error: conflict does not have a human approval request"
        approval = self.approvals.get_approval(decision.approval_id)
        if not approval or approval["status"] != "approved":
            return "Error: human approval is required before resolving this conflict"
        resolved = self.team.arbitrator.resolve_human(decision_id, selected_id)
        return json.dumps(asdict(resolved), ensure_ascii=False)

    def execute(self, name: str, args: dict) -> str:
        # Shared file-backed stores are intentionally serialized across agents.
        started = time.perf_counter()
        with self.tool_lock:
            output = self._execute_unlocked(name, args)
        trace_id = self.traces.current_trace_id
        failed = output.startswith("Error") or " unavailable" in output
        if trace_id:
            self.traces.record_span(
                trace_id, self._component_for_tool(name), name,
                "failed" if failed else "success",
                (time.perf_counter() - started) * 1000,
                error=output if failed else "",
                metadata={"agent_tool": True},
            )
        if failed and not name.startswith(("record_bad_case", "search_bad_cases",
                                           "replay_bad_case", "record_case_remediation")):
            try:
                self.bad_cases.collect(
                    self._case_type_for_tool(name), args,
                    [{"tool": name, "args": args}], output, name,
                )
            except Exception:
                pass
        return output

    @staticmethod
    def _component_for_tool(name: str) -> str:
        if name.startswith("mcp__"):
            return "mcp"
        if name in {"add_document", "search_knowledge"}:
            return "rag"
        if name in {"remember", "recall", "save_working", "add_evidence", "update_progress"}:
            return "memory"
        if any(token in name for token in ("subagent", "workflow", "conclusion")):
            return "collaboration"
        return "tool"

    @classmethod
    def _case_type_for_tool(cls, name: str) -> str:
        component = cls._component_for_tool(name)
        return {"rag": "retrieval", "memory": "memory",
                "collaboration": "agent_conflict"}.get(component, "tool")

    def _execute_unlocked(self, name: str, args: dict) -> str:
        handlers: dict[str, Callable] = {
            "add_document": self.knowledge.add_document,
            "search_knowledge": lambda **kw: json.dumps(self.knowledge.search_knowledge(**kw), ensure_ascii=False),
            "remember": self.memory.remember,
            "recall": lambda **kw: json.dumps(self.memory.recall(**kw), ensure_ascii=False),
            "save_working": lambda **kw: json.dumps(asdict(self.memory.save_working(**kw)), ensure_ascii=False),
            "add_evidence": lambda **kw: json.dumps(asdict(self.memory.add_evidence(**kw)), ensure_ascii=False),
            "update_progress": lambda **kw: json.dumps(asdict(self.memory.update_progress(**kw)), ensure_ascii=False),
            "create_task": lambda **kw: json.dumps(asdict(self.tasks.create_task(**kw)), ensure_ascii=False),
            "list_tasks": lambda: json.dumps([asdict(x) for x in self.tasks.list_tasks()], ensure_ascii=False),
            "claim_task": self.tasks.claim_task,
            "complete_task": self.tasks.complete_task,
            "request_approval": lambda **kw: json.dumps(asdict(self.approvals.request_approval(**kw)), ensure_ascii=False),
            "list_approvals": lambda: json.dumps(self.approvals.list_approvals(), ensure_ascii=False),
            "request_notification": lambda **kw: json.dumps(
                asdict(self.notifications.request_notification(**kw)), ensure_ascii=False),
            "deliver_notification": self.notifications.deliver_notification,
            "list_notifications": lambda: json.dumps(self.notifications.list_notifications(), ensure_ascii=False),
            "schedule_cron": lambda **kw: json.dumps(asdict(self.scheduler.schedule_job(**kw)), ensure_ascii=False),
            "list_crons": lambda: json.dumps([asdict(x) for x in self.scheduler.list_jobs()], ensure_ascii=False),
            "connect_mcp": self.connect_mcp,
            "spawn_subagent": lambda **kw: (self.team.spawn_subagent(**kw)
                                             if self.team else "Error: Agent team is not configured"),
            "collect_subagent_results": lambda: (self.team.collect_subagent_results()
                                                  if self.team else "No agent team configured"),
            "cancel_subagent": lambda **kw: (self.team.cancel_subagent(**kw)
                                               if self.team else "No agent team configured"),
            "reassign_subagent": lambda **kw: (self.team.reassign_failed(**kw)
                                                 if self.team else "No agent team configured"),
            "run_research_workflow": lambda **kw: (json.dumps(
                asdict(self.team.run_research_workflow(**kw)), ensure_ascii=False)
                if self.team else "No agent team configured"),
            "submit_conclusion": lambda **kw: (json.dumps(
                asdict(self.team.arbitrator.submit(**kw)), ensure_ascii=False)
                if self.team else "No agent team configured"),
            "list_conclusion_conflicts": lambda **kw: (json.dumps(
                [asdict(item) for item in self.team.arbitrator.conflicts(**kw)],
                ensure_ascii=False) if self.team else "No agent team configured"),
            "arbitrate_conclusions": lambda **kw: (json.dumps(
                asdict(self.team.arbitrator.arbitrate(**kw)), ensure_ascii=False)
                if self.team else "No agent team configured"),
            "promote_conclusion": lambda **kw: (self.team.arbitrator.promote(
                memory=self.memory, **kw) if self.team else "No agent team configured"),
            "list_workflow_checkpoints": lambda **kw: (json.dumps(
                self.team.store.list_snapshots(**kw), ensure_ascii=False)
                if self.team else "No agent team configured"),
            "rollback_workflow": lambda **kw: (json.dumps(
                asdict(self.team.rollback_workflow(**kw)), ensure_ascii=False)
                if self.team else "No agent team configured"),
            "resume_workflow": lambda **kw: (json.dumps(
                asdict(self.team.resume_workflow(**kw)), ensure_ascii=False)
                if self.team else "No agent team configured"),
            "record_bad_case": lambda **kw: json.dumps(
                asdict(self.bad_cases.collect(**kw)), ensure_ascii=False),
            "search_bad_cases": lambda **kw: json.dumps(
                self.bad_cases.search(**kw), ensure_ascii=False),
            "replay_bad_case": lambda **kw: json.dumps(
                self.bad_cases.replay(**kw), ensure_ascii=False),
            "record_case_remediation": lambda **kw: json.dumps(
                asdict(self.bad_cases.record_remediation(**kw)), ensure_ascii=False),
            "get_trace_report": lambda **kw: json.dumps(
                self.traces.report(**kw), ensure_ascii=False),
            "get_monitoring_metrics": lambda: json.dumps(
                self.traces.metrics(), ensure_ascii=False),
            "compare_agent_modes": lambda: json.dumps(
                self.traces.compare_modes(), ensure_ascii=False),
        }
        if name.startswith("mcp__"):
            parts = name.split("__", 2)
            if len(parts) != 3 or parts[1] not in self.mcp_clients:
                return f"Error: unknown MCP tool '{name}'"
            client = self.mcp_clients[parts[1]]
            definition = next((tool for tool in client.tools
                               if normalize_mcp_name(tool["name"]) == parts[2]), None)
            if not definition:
                return f"Error: unknown MCP tool '{name}'"
            read_only = definition.get("annotations", {}).get("readOnlyHint", False)
            if not read_only:
                request = self.approvals.request_approval(
                    "mcp_tool_call", {"server": parts[1], "tool": definition["name"], "args": args})
                return f"MCP call blocked pending approval: {request.id}"
            return self._call_mcp_with_fallback(parts[1], definition["name"], args)
        handler = handlers.get(name)
        if not handler: return f"Error: unknown tool '{name}'"
        try: return str(handler(**args))
        except Exception as exc: return f"Error: {type(exc).__name__}: {exc}"

    def execute_approved_action(self, request_id: str) -> str:
        approval = self.approvals.get_approval(request_id)
        if not approval:
            return f"Approval {request_id} not found"
        if approval["status"] != "approved":
            return f"Approval {request_id} is {approval['status']}, cannot execute"
        if approval["action"] != "mcp_tool_call":
            return f"Approval {request_id} has no executable MCP action"
        payload = approval["payload"]
        client = self.mcp_clients.get(payload["server"])
        if not client:
            return f"MCP server '{payload['server']}' is not connected"
        result = self._call_mcp_with_fallback(payload["server"], payload["tool"],
                                              payload.get("args", {}), mutating=True)
        self.approvals.mark_executed(request_id)
        return result


def _tool(name: str, description: str, properties: dict, required: list[str] | None = None) -> dict:
    return {"name": name, "description": description,
            "input_schema": {"type": "object", "properties": properties,
                             "required": required or []}}


TOOLS = [
    _tool("add_document", "Add a paper, note, or experiment record to the local knowledge base.",
          {"title": {"type": "string"}, "content": {"type": "string"}, "source": {"type": "string"}}, ["title", "content"]),
    _tool("search_knowledge", "Retrieve relevant chunks from the research knowledge base.",
          {"query": {"type": "string"}, "limit": {"type": "integer"}}, ["query"]),
    _tool("remember", "Persist an important research preference, fact, or conclusion.",
          {"content": {"type": "string"}, "category": {"type": "string"}}, ["content"]),
    _tool("recall", "Recall durable research memory.", {"query": {"type": "string"}, "limit": {"type": "integer"}}),
    _tool("save_working", "Save a task plan in disposable working memory.",
          {"task_id": {"type": "string"}, "plan": {"type": "array", "items": {"type": "string"}}}, ["task_id"]),
    _tool("add_evidence", "Attach sourced evidence to task working memory.",
          {"task_id": {"type": "string"}, "content": {"type": "string"},
           "source": {"type": "string"}, "confidence": {"type": "number"}},
          ["task_id", "content", "source"]),
    _tool("update_progress", "Update one planned step in working memory.",
          {"task_id": {"type": "string"}, "step": {"type": "string"},
           "status": {"type": "string"}}, ["task_id", "step", "status"]),
    _tool("create_task", "Create a durable research task.",
          {"subject": {"type": "string"}, "description": {"type": "string"},
           "blockedBy": {"type": "array", "items": {"type": "string"}}, "due_at": {"type": "string"}}, ["subject"]),
    _tool("list_tasks", "List the persistent research task board.", {}),
    _tool("claim_task", "Claim a startable task.", {"task_id": {"type": "string"}, "owner": {"type": "string"}}, ["task_id"]),
    _tool("complete_task", "Mark an in-progress task completed.", {"task_id": {"type": "string"}}, ["task_id"]),
    _tool("request_approval", "Request human approval before an external or important action.",
          {"action": {"type": "string"}, "payload": {"type": "object"}}, ["action", "payload"]),
    _tool("list_approvals", "List approval requests and decisions.", {}),
    _tool("request_notification", "Draft a notification and create a mandatory human approval request.",
          {"channel": {"type": "string"}, "recipient": {"type": "string"},
           "content": {"type": "string"}}, ["channel", "recipient", "content"]),
    _tool("deliver_notification", "Deliver an approved notification to the local outbox. Never bypass approval.",
          {"notification_id": {"type": "string"}}, ["notification_id"]),
    _tool("list_notifications", "List notification drafts and delivery status.", {}),
    _tool("schedule_cron", "Schedule durable research work using a five-field cron expression.",
          {"cron": {"type": "string"}, "prompt": {"type": "string"}, "recurring": {"type": "boolean"}}, ["cron", "prompt"]),
    _tool("list_crons", "List scheduled research jobs.", {}),
    _tool("connect_mcp", "Connect a preconfigured MCP server by name and discover its tools.",
          {"name": {"type": "string"}}, ["name"]),
    _tool("spawn_subagent", "Spawn a background research specialist for an independent task.",
          {"name": {"type": "string"}, "role": {"type": "string"},
           "prompt": {"type": "string"}}, ["name", "role", "prompt"]),
    _tool("collect_subagent_results", "Collect completed specialist-Agent results from the Lead inbox.", {}),
    _tool("cancel_subagent", "Request cooperative cancellation of a running specialist Agent.",
          {"name": {"type": "string"}}, ["name"]),
    _tool("reassign_subagent", "Reassign a failed, timed-out, or cancelled specialist task.",
          {"name": {"type": "string"}, "replacement": {"type": "string"}}, ["name"]),
    _tool("run_research_workflow", "Run the durable Planner-Researcher-Writer-Reviewer workflow.",
          {"research_question": {"type": "string"}}, ["research_question"]),
    _tool("submit_conclusion", "Submit a conclusion with evidence and confidence for conflict checks.",
          {"question": {"type": "string"}, "claim": {"type": "string"},
           "agent": {"type": "string"}, "evidence": {"type": "array", "items": {"type": "object"}},
           "confidence": {"type": "number"}}, ["question", "claim", "agent", "evidence", "confidence"]),
    _tool("list_conclusion_conflicts", "Find conflicting Agent conclusions for one question.",
          {"question": {"type": "string"}}, ["question"]),
    _tool("arbitrate_conclusions", "Ask the Lead to arbitrate evidence or request human review.",
          {"conclusion_ids": {"type": "array", "items": {"type": "string"}}}, ["conclusion_ids"]),
    _tool("promote_conclusion", "Promote only an approved conclusion into long-term memory.",
          {"decision_id": {"type": "string"}}, ["decision_id"]),
    _tool("list_workflow_checkpoints", "List recoverable checkpoints for a workflow.",
          {"workflow_id": {"type": "string"}}, ["workflow_id"]),
    _tool("rollback_workflow", "Restore a workflow to a selected checkpoint.",
          {"workflow_id": {"type": "string"}, "checkpoint_id": {"type": "string"}},
          ["workflow_id", "checkpoint_id"]),
    _tool("resume_workflow", "Continue a paused workflow from its last restored state.",
          {"workflow_id": {"type": "string"}}, ["workflow_id"]),
    _tool("record_bad_case", "Record a failed retrieval, citation, report, memory, collaboration, or output case.",
          {"case_type": {"type": "string"}, "task_input": {},
           "trajectory": {"type": "array", "items": {"type": "object"}},
           "result": {}, "failure_stage": {"type": "string"},
           "source": {"type": "string"}, "category": {"type": "string"}},
          ["case_type", "task_input", "result", "failure_stage"]),
    _tool("search_bad_cases", "Search the human-approved Bad Case library by type or category.",
          {"query": {"type": "string"}, "category": {"type": "string"},
           "case_type": {"type": "string"}, "accepted_only": {"type": "boolean"}}),
    _tool("replay_bad_case", "Load an accepted Bad Case as a deterministic replay package.",
          {"case_id": {"type": "string"}}, ["case_id"]),
    _tool("record_case_remediation", "Attach root cause, fixed version and retest result to a Bad Case.",
          {"case_id": {"type": "string"}, "root_cause": {"type": "string"},
           "fixed_version": {"type": "string"}, "retest_result": {"type": "string"}},
          ["case_id", "root_cause", "fixed_version", "retest_result"]),
    _tool("get_trace_report", "Get the complete spans, token, latency and cost report for one trace.",
          {"trace_id": {"type": "string"}}, ["trace_id"]),
    _tool("get_monitoring_metrics", "Get task success and component failure metrics.", {}),
    _tool("compare_agent_modes", "Compare single-Agent and multi-Agent quality, cost and latency.", {}),
]

TOOL_HANDLERS = {tool["name"]: tool["name"] for tool in TOOLS}


@dataclass
class RecoveryState:
    retries: int = 0


def with_retry(fn, state: RecoveryState, max_retries: int = 3, sleep: Callable = time.sleep):
    for attempt in range(max_retries):
        try: return fn()
        except Exception as exc:
            text = str(exc).lower()
            if not any(token in text for token in ("429", "529", "rate", "overloaded")):
                raise
            state.retries += 1
            if attempt + 1 == max_retries: raise
            sleep(2 ** attempt)


def build_system_prompt(workspace: Path) -> str:
    return ("You are a research workflow agent for a computer-science graduate student. "
            "Use retrieval before answering from stored research material. Persist only durable facts, "
            "manage tasks explicitly, and request approval before external communication or important changes. "
            "You cannot approve your own request; ask the human to use the CLI /approve command. "
            f"Workspace: {workspace.resolve()}")


def _message_text(message: dict) -> str:
    content = message.get("content", "")
    if isinstance(content, str):
        return content
    return " ".join(str(getattr(block, "text", "")) for block in content)


def agent_loop(client, messages: list, runtime: ResearchRuntime, model: str,
               max_rounds: int = 20, allowed_tool_names: set[str] | None = None,
               session_id: str = "default", agent_name: str = "lead",
               trace_id: str | None = None, mode: str = "single",
               task_type: str = "research_task") -> str:
    state = RecoveryState()
    owns_trace = trace_id is None
    trace_id = runtime.traces.start_trace(task_type, mode, trace_id)
    latest_query = next((_message_text(message) for message in reversed(messages)
                         if message.get("role") == "user"), "")
    if latest_query:
        runtime.memory.append_turn(session_id, "user", latest_query)
    messages[:] = runtime.memory.compress_messages(messages, session_id)
    memory_context = runtime.memory.build_context(latest_query, session_id)
    complexity = classify_complexity(latest_query)
    for _ in range(max_rounds):
        budget_state = runtime.traces.check_budget(trace_id)
        if budget_state:
            runtime.traces.finish_trace(trace_id, "paused", "budget exceeded")
            return ("Agent paused: trace budget exceeded; approve "
                    f"{budget_state.get('approval_id')} and use /continue")
        for job in runtime.scheduler.due_jobs():
            messages.append({"role": "user", "content": f"[Scheduled] {job.prompt}"})
        tools = runtime.assemble_tool_pool(allowed_tool_names)
        request = {
            "model": model,
            "system": (build_system_prompt(runtime.workspace)
                       + f"\nYour agent identity is {agent_name}."
                       + f"\nCurrent trace_id: {trace_id}."
                       + f"\nRelevant memory context: {memory_context}"),
            "messages": messages, "tools": tools, "max_tokens": 8000,
        }
        if hasattr(client, "create_for_task"):
            create = lambda: client.create_for_task(complexity, **request)
        else:
            create = lambda: client.messages.create(**request)
        model_started = time.perf_counter()
        try:
            response = with_retry(create, state)
        except Exception as exc:
            runtime.traces.record_model(trace_id, agent_name,
                                        (time.perf_counter() - model_started) * 1000,
                                        error=str(exc))
            if owns_trace:
                runtime.traces.finish_trace(trace_id, "failed", str(exc), 0.0)
            raise
        runtime.traces.record_model(trace_id, agent_name,
                                    (time.perf_counter() - model_started) * 1000,
                                    response=response)
        messages.append({"role": "assistant", "content": response.content})
        tool_blocks = [b for b in response.content if getattr(b, "type", None) == "tool_use"]
        if not tool_blocks:
            answer = "\n".join(getattr(b, "text", "") for b in response.content
                               if getattr(b, "type", None) == "text")
            runtime.memory.append_turn(session_id, "assistant", answer)
            if owns_trace:
                runtime.traces.finish_trace(trace_id, "completed", quality=1.0)
            return answer
        allowed = {tool["name"] for tool in tools}
        results = [{"type": "tool_result", "tool_use_id": block.id,
                    "content": (runtime.execute(block.name, block.input)
                                if block.name in allowed else "Error: tool not allowed")}
                   for block in tool_blocks]
        messages.append({"role": "user", "content": results})
    stopped = "Agent stopped: maximum rounds reached."
    runtime.bad_cases.collect("planning", latest_query, [], stopped,
                              "agent_loop", source="system")
    if owns_trace:
        runtime.traces.finish_trace(trace_id, "failed", stopped, 0.0)
    return stopped


def run_cli(workspace: Path | None = None) -> int:
    try:
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).with_name(".env"), override=True)
        from anthropic import Anthropic
    except ImportError as exc:
        print(f"Missing dependency: {exc}"); return 2
    workspace = (workspace or Path.cwd()).resolve()
    runtime = ResearchRuntime(workspace)
    model = os.getenv("MODEL_ID", MODEL)
    if not model: print("MODEL_ID is required"); return 2
    client = build_model_client(Anthropic, runtime.data_dir)
    team = AgentTeam(runtime, lambda: build_model_client(Anthropic, runtime.data_dir),
                     model, runner=agent_loop)
    runtime.bind_team(team)
    session_id = os.getenv("RESEARCH_SESSION_ID", "default")
    session = runtime.memory.load_session(session_id)
    messages = [{"role": item["role"], "content": item["content"]}
                for item in session.messages]
    print("Research Workflow Agent. Type q to quit.")
    last_query, last_answer, last_trace_id = "", "", None
    while True:
        try: query = input("research >> ").strip()
        except (EOFError, KeyboardInterrupt): return 0
        if query.lower() in ("", "q", "quit", "exit"): return 0
        if query.startswith("/approve ") or query.startswith("/reject "):
            command, request_id = query.split(maxsplit=1)
            print(runtime.approvals.review_approval(request_id, command == "/approve"))
            if command == "/approve":
                approval = runtime.approvals.get_approval(request_id)
                if approval and approval["action"] == "mcp_tool_call":
                    print(runtime.execute_approved_action(request_id))
            continue
        if query.startswith("/resolve "):
            _, decision_id, selected_id = query.split(maxsplit=2)
            print(runtime.resolve_conflict(decision_id, selected_id))
            continue
        if query == "/continue":
            if not last_trace_id:
                print("No paused trace to continue")
                continue
            last_answer = agent_loop(
                client, messages, runtime, model, session_id=session_id,
                trace_id=last_trace_id)
            print(last_answer)
            continue
        if query.startswith("/feedback "):
            parts = query.split(maxsplit=2)
            if len(parts) < 2 or parts[1] not in {"correction", "downvote", "rejection"}:
                print("Usage: /feedback correction|downvote|rejection [comment]")
                continue
            item = runtime.bad_cases.collect_feedback(
                parts[1], last_query, last_answer,
                parts[2] if len(parts) == 3 else "")
            print(f"Bad Case {item.id} saved; review approval: {item.approval_id}")
            continue
        if query.startswith("/review-case "):
            parts = query.split(maxsplit=3)
            if len(parts) != 4 or parts[2] not in {"approve", "reject"}:
                print("Usage: /review-case CASE_ID approve|reject REVIEWER")
                continue
            try:
                item = runtime.bad_cases.review(parts[1], parts[2] == "approve", parts[3])
                print(f"Bad Case {item.id} {item.review_status}")
            except (KeyError, PermissionError) as exc:
                print(f"Error: {exc}")
            continue
        messages.append({"role": "user", "content": query})
        last_query = query
        last_answer = agent_loop(client, messages, runtime, model, session_id=session_id)
        last_trace_id = runtime.traces.current_trace_id
        print(last_answer)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Research Workflow Agent")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args()
    raise SystemExit(run_cli(args.workspace))
