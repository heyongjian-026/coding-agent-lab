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
from typing import Callable


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


class MemoryStore:
    """Durable research preferences, conclusions and project facts."""

    def __init__(self, data_dir: Path):
        self.path = data_dir / "memory.json"

    def remember(self, content: str, category: str = "fact") -> str:
        items = _read_json(self.path, [])
        item = {"id": _id("mem"), "category": category, "content": content,
                "created_at": datetime.now().isoformat(timespec="seconds")}
        items.append(item)
        _write_json(self.path, items)
        return item["id"]

    def recall(self, query: str = "", limit: int = 10) -> list[dict]:
        items = _read_json(self.path, [])
        if query:
            words = _tokens(query)
            items = sorted(items, key=lambda x: len(words & _tokens(x["content"])), reverse=True)
            items = [item for item in items if words & _tokens(item["content"])]
        return items[-limit:]


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


def build_knowledge_base_from_env(data_dir: Path):
    """Select remote RAG when configured; otherwise retain the local teaching backend."""
    url = os.getenv("EMBEDDING_API_URL", "").strip()
    model = os.getenv("EMBEDDING_MODEL", "").strip()
    if not (url and model):
        return KnowledgeBase(data_dir)
    embedding = APIEmbeddingModel(
        url, os.getenv("EMBEDDING_API_KEY", ""), model,
        int(os.getenv("EMBEDDING_BATCH_SIZE", "10")),
        int(os.getenv("EMBEDDING_DIMENSION", "2048")),
    )
    return ElasticsearchKnowledgeBase(
        embedding, os.getenv("ELASTICSEARCH_HOST", "localhost"),
        int(os.getenv("ELASTICSEARCH_PORT", "9200")),
        os.getenv("ELASTICSEARCH_SCHEME", "http"),
        os.getenv("ELASTICSEARCH_USERNAME", "elastic"),
        os.getenv("ELASTICSEARCH_PASSWORD", ""),
    )


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
        return next((item for item in self.list_approvals() if item["id"] == request_id), None)

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


class MessageBus:
    """Thread-safe inboxes shared by Lead and research sub-Agents."""

    def __init__(self):
        self._inboxes: dict[str, list[dict]] = {}
        self._lock = threading.Lock()

    def send(self, sender: str, target: str, content: str, message_type: str = "message") -> None:
        with self._lock:
            self._inboxes.setdefault(target, []).append(
                {"from": sender, "to": target, "type": message_type, "content": content})

    def read_inbox(self, name: str) -> list[dict]:
        with self._lock:
            return self._inboxes.pop(name, [])


class AgentTeam:
    """Background specialist Agents; child Agents cannot spawn recursively."""

    def __init__(self, runtime, client_factory: Callable, model: str):
        self.runtime = runtime; self.client_factory = client_factory; self.model = model
        self.BUS = MessageBus(); self.active_teammates: dict[str, threading.Thread] = {}
        self._lock = threading.Lock()

    def spawn_subagent(self, name: str, role: str, prompt: str) -> str:
        safe_name = normalize_mcp_name(name)
        with self._lock:
            if safe_name in self.active_teammates:
                return f"Subagent '{safe_name}' already exists"

        def run():
            try:
                client = self.client_factory()
                messages = [{"role": "user", "content":
                             f"You are {safe_name}, specialist role: {role}. Task: {prompt}"}]
                allowed = {tool["name"] for tool in TOOLS} - {
                    "spawn_subagent", "collect_subagent_results", "connect_mcp",
                    "deliver_notification"}
                summary = agent_loop(client, messages, self.runtime, self.model,
                                     max_rounds=12, allowed_tool_names=allowed)
                self.BUS.send(safe_name, "lead", summary, "result")
            except Exception as exc:
                self.BUS.send(safe_name, "lead", f"Subagent error: {exc}", "error")
            finally:
                with self._lock: self.active_teammates.pop(safe_name, None)

        thread = threading.Thread(target=run, daemon=True, name=f"agent-{safe_name}")
        with self._lock: self.active_teammates[safe_name] = thread
        thread.start()
        return f"Subagent '{safe_name}' spawned as {role}"

    def collect_subagent_results(self) -> str:
        messages = self.BUS.read_inbox("lead")
        return json.dumps(messages, ensure_ascii=False) if messages else "No subagent results yet"


class ResearchRuntime:
    def __init__(self, workspace: Path, embedding_model=None):
        self.workspace = workspace.resolve()
        self.data_dir = self.workspace / DATA_DIR_NAME
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.tasks = TaskStore(self.data_dir)
        self.memory = MemoryStore(self.data_dir)
        self.knowledge = (KnowledgeBase(self.data_dir, embedding_model) if embedding_model
                          else build_knowledge_base_from_env(self.data_dir))
        self.approvals = ApprovalStore(self.data_dir)
        self.notifications = NotificationStore(self.data_dir, self.approvals)
        self.scheduler = CronScheduler(self.data_dir)
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

    def execute(self, name: str, args: dict) -> str:
        # Shared file-backed stores are intentionally serialized across agents.
        with self.tool_lock:
            return self._execute_unlocked(name, args)

    def _execute_unlocked(self, name: str, args: dict) -> str:
        handlers: dict[str, Callable] = {
            "add_document": self.knowledge.add_document,
            "search_knowledge": lambda **kw: json.dumps(self.knowledge.search_knowledge(**kw), ensure_ascii=False),
            "remember": self.memory.remember,
            "recall": lambda **kw: json.dumps(self.memory.recall(**kw), ensure_ascii=False),
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
            return client.call_tool(definition["name"], args)
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
        result = client.call_tool(payload["tool"], payload.get("args", {}))
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


def agent_loop(client, messages: list, runtime: ResearchRuntime, model: str,
               max_rounds: int = 20, allowed_tool_names: set[str] | None = None) -> str:
    state = RecoveryState()
    for _ in range(max_rounds):
        for job in runtime.scheduler.due_jobs():
            messages.append({"role": "user", "content": f"[Scheduled] {job.prompt}"})
        tools = runtime.assemble_tool_pool(allowed_tool_names)
        response = with_retry(lambda: client.messages.create(
            model=model, system=build_system_prompt(runtime.workspace), messages=messages,
            tools=tools, max_tokens=8000), state)
        messages.append({"role": "assistant", "content": response.content})
        tool_blocks = [b for b in response.content if getattr(b, "type", None) == "tool_use"]
        if not tool_blocks:
            return "\n".join(getattr(b, "text", "") for b in response.content
                             if getattr(b, "type", None) == "text")
        allowed = {tool["name"] for tool in tools}
        results = [{"type": "tool_result", "tool_use_id": block.id,
                    "content": (runtime.execute(block.name, block.input)
                                if block.name in allowed else "Error: tool not allowed")}
                   for block in tool_blocks]
        messages.append({"role": "user", "content": results})
    return "Agent stopped: maximum rounds reached."


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
    client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"),
                       base_url=os.getenv("ANTHROPIC_BASE_URL"))
    team = AgentTeam(runtime, lambda: Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"),
                                                base_url=os.getenv("ANTHROPIC_BASE_URL")), model)
    runtime.bind_team(team)
    messages = []
    print("Research Workflow Agent. Type q to quit.")
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
        messages.append({"role": "user", "content": query})
        print(agent_loop(client, messages, runtime, model))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Research Workflow Agent")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args()
    raise SystemExit(run_cli(args.workspace))
