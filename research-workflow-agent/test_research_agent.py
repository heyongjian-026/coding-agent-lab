from datetime import datetime
from types import SimpleNamespace
import json

import research_agent as ra


def test_task_dependencies_and_lifecycle(tmp_path):
    store = ra.TaskStore(tmp_path)
    first = store.create_task("阅读论文")
    second = store.create_task("复现实验", blockedBy=[first.id])
    assert not store.can_start(second.id)
    assert "Claimed" in store.claim_task(first.id, "reader")
    assert "Completed" in store.complete_task(first.id)
    assert store.can_start(second.id)


def test_memory_is_persistent_and_searchable(tmp_path):
    store = ra.MemoryStore(tmp_path)
    store.remember("研究方向是 Coding Agent 上下文压缩", "research_direction")
    again = ra.MemoryStore(tmp_path)
    assert again.recall("上下文压缩")[0]["category"] == "research_direction"


def test_memory_query_limit_keeps_most_relevant_and_empty_query_keeps_latest(tmp_path):
    store = ra.MemoryStore(tmp_path)
    store.remember("agent context compression", "most_relevant")
    store.remember("agent database", "less_relevant")
    store.remember("unrelated note", "latest")

    assert store.recall("agent context", limit=1)[0]["category"] == "most_relevant"
    assert store.recall(limit=1)[0]["category"] == "latest"


def test_local_rag_retrieval(tmp_path):
    kb = ra.KnowledgeBase(tmp_path)
    kb.add_document("Agent论文", "上下文压缩能够减少长对话的 token 消耗。")
    kb.add_document("数据库论文", "索引能够提高查询速度。")
    hits = kb.search_knowledge("上下文压缩 token")
    assert hits[0]["title"] == "Agent论文"
    stored = (tmp_path / "knowledge.json").read_text(encoding="utf-8")
    assert '"embedding"' in stored


def test_vector_rag_accepts_injected_embedding_model(tmp_path):
    class FakeEmbedding:
        def embed(self, text):
            return [1.0, 0.0] if "agent" in text.lower() else [0.0, 1.0]

    kb = ra.KnowledgeBase(tmp_path, FakeEmbedding())
    kb.add_document("Agent", "agent context")
    kb.add_document("Database", "database index")
    assert kb.search_knowledge("agent")[0]["title"] == "Agent"


def test_api_embedding_batches_dashscope_requests():
    calls = []

    class Response:
        def __init__(self, value):
            self.value = value
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def read(self):
            return json.dumps(self.value).encode()

    def opener(request, timeout):
        payload = json.loads(request.data)
        calls.append(payload)
        data = [{"index": i, "embedding": [float(i), 1.0]}
                for i, _ in enumerate(payload["input"])]
        return Response({"data": data})

    model = ra.APIEmbeddingModel("https://example.test/v1", "secret", "embedding",
                                 batch_size=2, dimensions=2, opener=opener)
    assert len(model.embed_many(["a", "b", "c"])) == 3
    assert [len(call["input"]) for call in calls] == [2, 1]
    assert all(call["dimensions"] == 2 for call in calls)


def test_elasticsearch_knowledge_base_indexes_and_searches():
    calls = []

    class Embedding:
        dimensions = 2
        def embed_many(self, texts):
            return [[1.0, 0.0] for _ in texts]
        def embed(self, text):
            return [1.0, 0.0]

    class Response:
        def __init__(self, value=None):
            self.value = value or {}
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def read(self):
            return json.dumps(self.value).encode()

    def opener(request, timeout):
        payload = json.loads(request.data) if request.data else None
        calls.append((request.method, request.full_url, payload))
        if request.full_url.endswith("/_search"):
            return Response({"hits": {"hits": [{"_score": 1.0, "_source": {
                "document_id": "doc_1", "title": "Agent", "source": "manual",
                "chunk": 0, "content": "context compression"}}]}})
        return Response()

    kb = ra.ElasticsearchKnowledgeBase(Embedding(), opener=opener)
    kb.add_document("Agent", "context compression")
    hits = kb.search_knowledge("context")
    assert hits[0]["title"] == "Agent"
    assert any(method == "PUT" and "/_doc/" in url for method, url, _ in calls)
    search = next(payload for method, url, payload in calls if url.endswith("/_search"))
    assert search["knn"]["query_vector"] == [1.0, 0.0]


def test_approval_lifecycle(tmp_path):
    store = ra.ApprovalStore(tmp_path)
    request = store.request_approval("send_weekly_report", {"to": "advisor"})
    assert request.status == "pending"
    assert "approved" in store.review_approval(request.id, True)
    assert store.list_approvals()[0]["status"] == "approved"


def test_notification_cannot_deliver_before_human_approval(tmp_path):
    approvals = ra.ApprovalStore(tmp_path)
    notifications = ra.NotificationStore(tmp_path, approvals)
    note = notifications.request_notification("email", "advisor@example.com", "本周研究周报")
    assert "blocked" in notifications.deliver_notification(note.id)
    approvals.review_approval(note.approval_id, True)
    assert "Delivered" in notifications.deliver_notification(note.id)
    assert "本周研究周报" in (tmp_path / "notification_outbox.jsonl").read_text(encoding="utf-8")


def test_cron_schedule_and_deduplicate_minute(tmp_path):
    scheduler = ra.CronScheduler(tmp_path)
    scheduler.schedule_job("30 9 * * 1", "生成周报")
    now = datetime(2026, 8, 10, 9, 30)  # Monday
    assert len(scheduler.due_jobs(now)) == 1
    assert scheduler.due_jobs(now) == []


def test_cron_rejects_zero_step():
    assert ra.validate_cron("*/0 * * * *") is not None


def test_runtime_dispatch(tmp_path):
    runtime = ra.ResearchRuntime(tmp_path)
    result = runtime.execute("remember", {"content": "偏好中文回答"})
    assert result.startswith("mem_")
    assert "unknown tool" in runtime.execute("missing", {})


def test_tools_keep_s20_style_names():
    names = {tool["name"] for tool in ra.TOOLS}
    assert {"create_task", "claim_task", "complete_task", "schedule_cron"} <= names


class FakeMCPTransport:
    def __init__(self):
        self.calls = []

    def request(self, method, params=None):
        self.calls.append((method, params or {}))
        if method == "initialize":
            return {"protocolVersion": "test"}
        if method == "tools/list":
            return {"tools": [{"name": "search papers", "description": "Search papers",
                                "annotations": {"readOnlyHint": True},
                                "inputSchema": {"type": "object", "properties": {
                                    "query": {"type": "string"}}, "required": ["query"]}}]}
        if method == "tools/call":
            return {"content": [{"type": "text", "text": "paper result"}]}
        return {}

    def notify(self, method, params=None):
        self.calls.append((method, params or {}))


def test_mcp_connect_discover_and_dispatch(tmp_path):
    runtime = ra.ResearchRuntime(tmp_path)
    transport = FakeMCPTransport()
    assert "Discovered 1" in runtime.connect_mcp("paper docs", transport)
    names = {tool["name"] for tool in runtime.assemble_tool_pool()}
    assert "mcp__paper_docs__search_papers" in names
    result = runtime.execute("mcp__paper_docs__search_papers", {"query": "agents"})
    assert result == "paper result"
    assert any(method == "tools/call" for method, _ in transport.calls)


def test_connect_mcp_rejects_unconfigured_server(tmp_path):
    runtime = ra.ResearchRuntime(tmp_path)
    assert "not configured" in runtime.connect_mcp("unknown")


def test_mutating_mcp_tool_requires_human_approval(tmp_path):
    class MutatingTransport(FakeMCPTransport):
        def request(self, method, params=None):
            if method == "tools/list":
                return {"tools": [{"name": "write_note", "inputSchema": {"type": "object"}}]}
            return super().request(method, params)

    runtime = ra.ResearchRuntime(tmp_path)
    runtime.connect_mcp("zotero", MutatingTransport())
    blocked = runtime.execute("mcp__zotero__write_note", {"text": "draft"})
    assert "pending approval" in blocked
    request_id = blocked.rsplit(" ", 1)[-1]
    runtime.approvals.review_approval(request_id, True)
    assert runtime.execute_approved_action(request_id) == "paper result"
    assert runtime.approvals.get_approval(request_id)["status"] == "executed"


def test_subagent_returns_result_to_lead(tmp_path):
    class Messages:
        def create(self, **kwargs):
            return SimpleNamespace(content=[SimpleNamespace(type="text", text="文献分析完成")],
                                   stop_reason="end_turn")

    client = SimpleNamespace(messages=Messages())
    runtime = ra.ResearchRuntime(tmp_path)
    team = ra.AgentTeam(runtime, lambda: client, "test-model")
    runtime.bind_team(team)
    assert "spawned" in team.spawn_subagent("paper-reader", "文献分析", "总结论文")
    team.active_teammates["paper-reader"].join(timeout=2)
    assert "文献分析完成" in team.collect_subagent_results()


def test_subagent_tool_pool_cannot_spawn_recursively(tmp_path):
    runtime = ra.ResearchRuntime(tmp_path)
    restricted = runtime.assemble_tool_pool({"search_knowledge"})
    assert {tool["name"] for tool in restricted} == {"search_knowledge"}
