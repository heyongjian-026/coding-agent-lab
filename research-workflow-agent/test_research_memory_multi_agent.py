import json
import threading
import time
from types import SimpleNamespace

import pytest

import research_agent as ra


def test_three_tiers_are_separate_and_persistent(tmp_path):
    memory = ra.MemoryStore(tmp_path)
    memory.save_working("task-1", ["search", "write"], progress={"search": "pending"})
    memory.add_evidence("task-1", "paper result", "doi:1", 0.8)
    memory.append_turn("session-1", "user", "研究 Agent memory")
    memory.remember("用户偏好中文", "preference", source="user", confidence=0.9)

    again = ra.MemoryStore(tmp_path)
    assert again.load_working("task-1").evidence[0]["source"] == "doi:1"
    assert again.load_session("session-1").messages[0]["role"] == "user"
    assert again.recall("偏好中文")[0]["confidence"] == 0.9
    assert {"working_memory.json", "short_term_memory.json", "long_term_memory.json"} <= {
        path.name for path in tmp_path.iterdir()
    }


def test_long_term_memory_requires_trust_and_supports_correction(tmp_path):
    memory = ra.MemoryStore(tmp_path)
    with pytest.raises(ValueError, match="requires confirmation"):
        memory.remember("未经核实的检索结果", source="retrieval", confirmed=False)
    first = memory.remember("model A accuracy is 80%", "confirmed_conclusion")
    conflicts = memory.detect_conflicts("model A accuracy is 82%", "confirmed_conclusion")
    assert conflicts[0]["id"] == first
    corrected = memory.update_memory(first, "model A accuracy is 82%", source="reviewer")
    assert memory.recall("accuracy")[0]["supersedes"] == [first]
    assert memory.forget(corrected)


def test_memory_eviction_and_context_compression(tmp_path):
    memory = ra.MemoryStore(tmp_path, max_long_term=2)
    memory.remember("low confidence", confidence=0.1)
    memory.remember("high confidence", confidence=0.9)
    memory.remember("medium confidence", confidence=0.5)
    assert {item["content"] for item in memory.recall()} == {
        "high confidence", "medium confidence"
    }
    messages = [{"role": "user", "content": f"turn {index}"} for index in range(15)]
    compressed = memory.compress_messages(messages, "resume-me")
    assert len(compressed) == 7
    assert "turn 0" in memory.load_session("resume-me").summary


def _text_runner(client, messages, runtime, model, **kwargs):
    role = kwargs.get("agent_name")
    payloads = {
        "planner": {"plan": ["search", "write"], "success_criteria": ["cited"]},
        "researcher": {"evidence": [{"content": "finding", "source": "doi:1",
                                       "confidence": 0.8}], "confidence": 0.8},
        "writer": {"draft": "evidence-backed draft", "citations": ["doi:1"]},
        "reviewer": {"approved": True, "issues": [], "final": "final report"},
    }
    return json.dumps(payloads[role]) if role in payloads else f"{role} completed"


def test_role_protocols_and_tool_permissions_are_explicit():
    assert set(ra.ROLE_SPECS) == {"planner", "researcher", "writer", "reviewer"}
    assert "spawn_subagent" not in ra.ROLE_SPECS["researcher"].allowed_tools
    assert ra.ROLE_SPECS["reviewer"].output_fields == ("approved", "issues", "final")


def test_role_result_protocol_rejects_missing_fields():
    with pytest.raises(ValueError, match="missing fields"):
        ra.parse_role_result("planner", "planner", '{"plan": []}')


def test_agent_identity_status_and_result_are_persistent(tmp_path):
    runtime = ra.ResearchRuntime(tmp_path)
    team = ra.AgentTeam(runtime, lambda: object(), "model", runner=_text_runner)
    runtime.bind_team(team)
    assert "spawned" in team.spawn_subagent("reader", "researcher", "find evidence")
    team.active_teammates["reader"].join(timeout=2)
    stored = team.store.load_agent("reader")
    assert stored.status == "completed"
    assert stored.role == "researcher"
    assert "completed" in team.collect_subagent_results()


def test_concurrency_limit_and_cancellation(tmp_path):
    release = threading.Event()

    def blocking_runner(*args, **kwargs):
        release.wait(timeout=1)
        return "done"

    runtime = ra.ResearchRuntime(tmp_path)
    team = ra.AgentTeam(runtime, lambda: object(), "model", runner=blocking_runner,
                        max_concurrency=1)
    assert "spawned" in team.spawn_subagent("one", "researcher", "task")
    assert "maximum 1" in team.spawn_subagent("two", "writer", "task")
    assert "cancellation requested" in team.cancel_subagent("one")
    release.set()
    team.active_teammates["one"].join(timeout=2)
    assert team.store.load_agent("one").status == "cancelled"


def test_failed_task_can_be_reassigned(tmp_path):
    calls = []

    def runner(*args, **kwargs):
        calls.append(kwargs["agent_name"])
        if len(calls) == 1:
            raise RuntimeError("temporary failure")
        return "recovered"

    runtime = ra.ResearchRuntime(tmp_path)
    team = ra.AgentTeam(runtime, lambda: object(), "model", runner=runner)
    team.spawn_subagent("reader", "researcher", "task")
    team.active_teammates["reader"].join(timeout=2)
    assert "spawned" in team.reassign_failed("reader", "reader-backup")
    team.active_teammates["reader-backup"].join(timeout=2)
    assert team.store.load_agent("reader-backup").status == "completed"


def test_end_to_end_role_workflow_has_durable_checkpoints(tmp_path):
    runtime = ra.ResearchRuntime(tmp_path)
    team = ra.AgentTeam(runtime, lambda: object(), "model", runner=_text_runner)
    state = team.run_research_workflow("Agent memory 如何评测？")
    assert state.status == "completed"
    assert [task.role for task in state.tasks] == [
        "planner", "researcher", "writer", "reviewer"
    ]
    assert all(task.status == "completed" for task in state.tasks)
    restored = team.store.load_workflow(state.id)
    assert json.loads(restored.results["reviewer"])["approved"] is True
    assert runtime.memory.load_working(state.id).progress["reviewer"] == "completed"
    assert runtime.memory.load_working(state.id).evidence[0]["source"] == "doi:1"
    events = (runtime.data_dir / "workflow_events.jsonl").read_text(encoding="utf-8")
    assert "workflow_step_completed" in events


def test_agent_loop_recalls_memory_and_persists_answer(tmp_path):
    captured = {}

    class Messages:
        def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(content=[SimpleNamespace(type="text", text="回答")])

    runtime = ra.ResearchRuntime(tmp_path)
    runtime.memory.remember("用户偏好中文", "preference")
    messages = [{"role": "user", "content": "请按偏好回答"}]
    answer = ra.agent_loop(SimpleNamespace(messages=Messages()), messages, runtime,
                           "model", session_id="s1")
    assert answer == "回答"
    assert "用户偏好中文" in captured["system"]
    assert runtime.memory.load_session("s1").messages[-1]["content"] == "回答"
