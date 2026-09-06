import json
from types import SimpleNamespace

import pytest

import research_agent as ra
from resilience import (FallbackEmbeddingModel, FallbackJournal,
                        FallbackKnowledgeBase, ModelEndpoint,
                        ResilientModelClient, classify_complexity)


def _workflow_payload(role):
    return {
        "planner": {"plan": ["search", "write"], "success_criteria": ["cited"]},
        "researcher": {"evidence": [{"content": "finding", "source": "doi:1",
                                       "confidence": 0.8}], "confidence": 0.8},
        "writer": {"draft": "draft", "citations": ["doi:1"]},
        "reviewer": {"approved": True, "issues": [], "final": "report"},
    }[role]


def _successful_runner(client, messages, runtime, model, **kwargs):
    return json.dumps(_workflow_payload(kwargs["agent_name"]))


def test_conflicts_require_evidence_and_approved_result_can_be_promoted(tmp_path):
    runtime = ra.ResearchRuntime(tmp_path)
    team = ra.AgentTeam(runtime, lambda: object(), "model", runner=_successful_runner)
    runtime.bind_team(team)
    with pytest.raises(ValueError, match="sourced evidence"):
        team.arbitrator.submit("Which model?", "A", "agent-a", [], 0.9)
    left = team.arbitrator.submit(
        "Which model?", "Use model A", "agent-a",
        [{"content": "benchmark", "source": "doi:a", "confidence": 0.9}], 0.9)
    right = team.arbitrator.submit(
        "Which model?", "Use model B", "agent-b",
        [{"content": "benchmark", "source": "doi:b", "confidence": 0.5}], 0.5)
    assert {item.id for item in team.arbitrator.conflicts("Which model?")} == {
        left.id, right.id
    }
    decision = team.arbitrator.arbitrate([left.id, right.id])
    assert decision.status == "approved" and decision.selected_id == left.id
    memory_id = team.arbitrator.promote(decision.id, runtime.memory)
    assert runtime.memory.recall("model A")[0]["id"] == memory_id


def test_close_conflict_requires_real_human_approval(tmp_path):
    runtime = ra.ResearchRuntime(tmp_path)
    team = ra.AgentTeam(runtime, lambda: object(), "model", runner=_successful_runner)
    runtime.bind_team(team)
    left = team.arbitrator.submit("Q", "A", "one", [{"source": "s1"}], 0.80)
    right = team.arbitrator.submit("Q", "B", "two", [{"source": "s2"}], 0.75)
    decision = team.arbitrator.arbitrate([left.id, right.id])
    assert decision.status == "human_required" and decision.approval_id
    with pytest.raises(ValueError, match="Only an approved"):
        team.arbitrator.promote(decision.id, runtime.memory)
    assert "human approval is required" in runtime.resolve_conflict(decision.id, right.id)
    runtime.approvals.review_approval(decision.approval_id, True)
    assert '"status": "approved"' in runtime.resolve_conflict(decision.id, right.id)


def test_one_role_failure_is_retried_without_stopping_workflow(tmp_path):
    attempts = {}

    def flaky_runner(client, messages, runtime, model, **kwargs):
        role = kwargs["agent_name"]
        attempts[role] = attempts.get(role, 0) + 1
        if role == "researcher" and attempts[role] == 1:
            raise RuntimeError("temporary researcher failure")
        return json.dumps(_workflow_payload(role))

    runtime = ra.ResearchRuntime(tmp_path)
    team = ra.AgentTeam(runtime, lambda: object(), "model", runner=flaky_runner)
    state = team.run_research_workflow("research question")
    assert state.status == "completed"
    assert attempts["researcher"] == 2
    assert len(team.store.list_snapshots(state.id)) == 4


def test_failed_step_rolls_back_locally_and_can_resume(tmp_path):
    def failing_runner(client, messages, runtime, model, **kwargs):
        role = kwargs["agent_name"]
        if role == "researcher":
            raise RuntimeError("source unavailable")
        return json.dumps(_workflow_payload(role))

    runtime = ra.ResearchRuntime(tmp_path)
    team = ra.AgentTeam(runtime, lambda: object(), "model", runner=failing_runner)
    paused = team.run_research_workflow("research question")
    assert paused.status == "paused"
    assert paused.tasks[0].status == "completed"
    assert paused.tasks[1].status == "pending"
    team.runner = _successful_runner
    resumed = team.resume_workflow(paused.id)
    assert resumed.status == "completed"


class _Messages:
    def __init__(self, result=None, error=None):
        self.result, self.error, self.calls = result, error, []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.result


def test_model_router_uses_complexity_and_falls_back(tmp_path):
    primary_messages = _Messages(error=RuntimeError("service unavailable"))
    fallback_messages = _Messages(result="fallback-result")
    light_messages = _Messages(result="light-result")
    primary = SimpleNamespace(messages=primary_messages)
    fallback = SimpleNamespace(messages=fallback_messages)
    light = SimpleNamespace(messages=light_messages)
    router = ResilientModelClient([
        ModelEndpoint("primary", primary, "strong-1", "strong", True),
        ModelEndpoint("fallback", fallback, "strong-2", "strong"),
        ModelEndpoint("light", light, "light-1", "light"),
    ], FallbackJournal(tmp_path))
    assert router.create_for_task("light", messages=[]) == "light-result"
    assert router.create_for_task("strong", messages=[]) == "fallback-result"
    assert fallback_messages.calls[0]["model"] == "strong-2"
    assert classify_complexity("简单问题") == "light"
    assert classify_complexity("请综合比较多篇论文的架构") == "strong"


def test_embedding_and_knowledge_fall_back_with_idempotent_writes(tmp_path):
    class BrokenEmbedding:
        dimensions = 2
        def embed_many(self, texts):
            raise RuntimeError("embedding offline")

    class LocalEmbedding:
        dimensions = 2
        def embed_many(self, texts):
            return [[1.0, 0.0] for _ in texts]

    journal = FallbackJournal(tmp_path)
    embedding = FallbackEmbeddingModel(BrokenEmbedding(), LocalEmbedding(), journal)
    assert embedding.embed_many(["a"]) == [[1.0, 0.0]]

    class BrokenKB:
        def add_document(self, **kwargs):
            raise RuntimeError("elasticsearch offline")
        def search_knowledge(self, query, limit):
            raise RuntimeError("elasticsearch offline")

    class LocalKB:
        def __init__(self):
            self.writes = 0
        def add_document(self, **kwargs):
            self.writes += 1
            return "local-doc"
        def search_knowledge(self, query, limit):
            return [{"content": "local hit"}]

    local = LocalKB()
    knowledge = FallbackKnowledgeBase(BrokenKB(), local, journal)
    assert knowledge.add_document("title", "content") == "local-doc"
    assert knowledge.add_document("title", "content") == "local-doc"
    assert local.writes == 1
    assert knowledge.search_knowledge("query")[0]["content"] == "local hit"


class _MCPTransport:
    def __init__(self, fail=False):
        self.fail = fail

    def request(self, method, params=None):
        if method == "tools/list":
            return {"tools": [{"name": "search", "annotations": {"readOnlyHint": True},
                                "inputSchema": {"type": "object"}}]}
        if method == "tools/call":
            if self.fail:
                raise RuntimeError("MCP unavailable")
            return {"content": [{"type": "text", "text": "backup result"}]}
        return {}

    def notify(self, method, params=None):
        pass


def test_mcp_switches_source_or_deduplicates_pending_task(tmp_path):
    runtime = ra.ResearchRuntime(tmp_path)
    runtime.connect_mcp("primary", _MCPTransport(fail=True))
    runtime.connect_mcp("backup", _MCPTransport())
    (runtime.data_dir / "mcp_servers.json").write_text(
        json.dumps({"primary": {"fallback_servers": ["backup"]}}), encoding="utf-8")
    assert runtime.execute("mcp__primary__search", {"q": "agents"}) == "backup result"

    runtime.mcp_clients.pop("backup")
    first = runtime.execute("mcp__primary__search", {"q": "offline"})
    second = runtime.execute("mcp__primary__search", {"q": "offline"})
    assert "pending task" in first and first == second
    pending = json.loads((runtime.data_dir / "pending_external_tasks.json").read_text())
    assert len(pending) == 1
    assert "fallback" in (runtime.data_dir / "fallback_events.jsonl").read_text()
