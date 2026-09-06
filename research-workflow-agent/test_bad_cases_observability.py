import json
from types import SimpleNamespace

import pytest

import research_agent as ra
from observability import TraceBudget, TraceStore


def test_bad_case_feedback_is_redacted_reviewed_searched_and_replayed(tmp_path):
    runtime = ra.ResearchRuntime(tmp_path)
    case = runtime.bad_cases.collect_feedback(
        "correction",
        {"question": "contact me@example.com", "token": "secret-value"},
        "Bearer abcdefghijklmnop",
        "answer was wrong",
        [{"tool": "search_knowledge", "args": {"phone": "13812345678"}}],
    )
    raw = json.dumps(case.task_input, ensure_ascii=False)
    assert "secret-value" not in raw
    assert "me@example.com" not in raw
    assert "13812345678" not in json.dumps(case.trajectory)
    with pytest.raises(PermissionError):
        runtime.bad_cases.review(case.id, True, "lead")

    runtime.approvals.review_approval(case.approval_id, True)
    accepted = runtime.bad_cases.review(case.id, True, "lead")
    assert accepted.review_status == "accepted"
    runtime.bad_cases.record_remediation(case.id, "missing evidence", "v2", "passed")
    assert runtime.bad_cases.search("missing evidence", category="output")[0]["id"] == case.id
    replay = runtime.bad_cases.replay(case.id)
    assert replay["case_id"] == case.id


def test_bad_case_types_cover_research_failures(tmp_path):
    runtime = ra.ResearchRuntime(tmp_path)
    expected = {
        "retrieval": "retrieval", "citation": "output", "weekly_report": "output",
        "memory": "memory", "agent_conflict": "collaboration",
    }
    for case_type, category in expected.items():
        item = runtime.bad_cases.collect(case_type, "input", [], "bad", case_type)
        assert item.category == category


def test_runtime_failure_creates_trace_span_and_candidate_case(tmp_path):
    runtime = ra.ResearchRuntime(tmp_path)
    trace_id = runtime.traces.start_trace()
    output = runtime.execute("unknown_tool", {})
    runtime.traces.finish_trace(trace_id, "failed", output)
    report = runtime.traces.report(trace_id)
    assert report["spans"][0]["component"] == "tool"
    assert report["spans"][0]["status"] == "failed"
    candidates = runtime.bad_cases.search(accepted_only=False)
    assert candidates[0]["case_type"] == "tool"


def test_tokens_cost_budget_pause_and_human_override(tmp_path, monkeypatch):
    monkeypatch.setenv("MODEL_INPUT_PRICE_PER_MILLION", "2")
    monkeypatch.setenv("MODEL_OUTPUT_PRICE_PER_MILLION", "4")
    approvals = {}

    def request(payload):
        approvals["approval_1"] = False
        return "approval_1"

    store = TraceStore(tmp_path, TraceBudget(max_tokens=3), request,
                       lambda approval_id: approvals[approval_id])
    trace_id = store.start_trace()
    response = SimpleNamespace(usage=SimpleNamespace(input_tokens=2, output_tokens=2))
    store.record_model(trace_id, "lead", 12.0, response=response)
    budget = store.check_budget(trace_id)
    assert budget["exceeded"] == ["tokens"]
    assert budget["approval_id"] == "approval_1"
    approvals["approval_1"] = True
    assert store.check_budget(trace_id) is None
    store.finish_trace(trace_id, "completed", quality=0.9)
    trace = store.report(trace_id)["trace"]
    assert trace["input_tokens"] == 2 and trace["output_tokens"] == 2
    assert trace["cost"] == pytest.approx(0.000012)


def test_metrics_and_single_multi_comparison(tmp_path):
    store = TraceStore(tmp_path)
    single = store.start_trace("weekly_report", "single")
    store.record_span(single, "rag", "search", "failed", 3.0, error="offline")
    store.finish_trace(single, "failed", "offline", quality=0.2)
    multi = store.start_trace("research_task", "multi")
    store.record_span(multi, "subtask", "researcher", "success", 2.0,
                      metadata={"attempt": 2, "status": "completed"})
    store.finish_trace(multi, "completed", quality=0.8)

    metrics = store.metrics()
    comparison = store.compare_modes()
    assert metrics["task_success_rate"] == 0.5
    assert metrics["weekly_report_success_rate"] == 0.0
    assert metrics["component_failure_rate"]["rag"] == 1.0
    assert comparison["single"]["quality"] == 0.2
    assert comparison["multi"]["quality"] == 0.8


def test_agent_loop_records_model_tokens_and_finishes_trace(tmp_path):
    runtime = ra.ResearchRuntime(tmp_path)
    response = SimpleNamespace(
        content=[SimpleNamespace(type="text", text="done")],
        usage=SimpleNamespace(input_tokens=5, output_tokens=2),
    )
    client = SimpleNamespace(messages=SimpleNamespace(create=lambda **kwargs: response))
    answer = ra.agent_loop(client, [{"role": "user", "content": "summarize"}],
                           runtime, "test-model")
    assert answer == "done"
    traces = json.loads(runtime.traces.traces_path.read_text(encoding="utf-8"))
    trace = next(iter(traces.values()))
    assert trace["status"] == "completed"
    assert trace["input_tokens"] == 5
