import json

import eval_runner as er
from coding_agent import CommandRequest


def test_eval_sandbox_rejects_non_allowlisted_command(tmp_path):
    result = er.EvalSandbox().execute(CommandRequest(["python", "unsafe.py"]), tmp_path)
    assert result.returncode == -1
    assert "not allowlisted" in result.stderr


def test_calculate_metrics_covers_quality_cost_and_safety():
    result = er.CaseResult("x", "safety", True, True, False, True, 2, 2, 1, True, [])
    metrics = er.calculate_metrics([result])
    assert metrics["task_completion_rate"] == 1.0
    assert metrics["test_pass_rate"] == 1.0
    assert metrics["argument_accuracy"] == 1.0
    assert metrics["average_repair_rounds"] == 1.0
    assert metrics["dangerous_action_block_rate"] == 1.0


def test_compare_baseline_detects_both_metric_directions():
    baseline = {
        "metrics": {"task_completion_rate": 1.0, "average_repair_rounds": 0.0},
        "tolerances": {"task_completion_rate": 0.01, "average_repair_rounds": 0.1},
    }
    regressions = er.compare_baseline(
        {"task_completion_rate": 0.8, "average_repair_rounds": 1.0}, baseline
    )
    assert len(regressions) == 2


def test_run_suite_rejects_dataset_smaller_than_minimum(tmp_path):
    dataset = {
        "suite": "tiny",
        "cases": [{
            "id": "read", "category": "bug_fix", "prompt": "Read a file",
            "initial_files": {"a.py": "x = 1\n"},
            "trajectory": [{"tool": "read_file", "args": {"path": "a.py"}}],
            "expected_tools": ["read_file"],
            "acceptance": {"outputs_contain": ["x = 1"], "changed_files": []},
        }],
    }
    dataset_path = tmp_path / "tasks.json"
    output_path = tmp_path / "result.json"
    dataset_path.write_text(json.dumps(dataset), encoding="utf-8")
    try:
        er.run_suite(dataset_path, output_path)
    except ValueError as error:
        assert "10 to 20" in str(error)
    else:
        raise AssertionError("undersized dataset should be rejected")
