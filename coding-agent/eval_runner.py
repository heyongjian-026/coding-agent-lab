"""Deterministic offline replay evaluation for the coding-agent harness."""

from __future__ import annotations

import argparse
import json
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from coding_agent import CommandRequest, CommandResult, LocalBackend, ToolRuntime, classify_tool_failure

REQUIRED_CATEGORIES = {"bug_fix", "feature", "test_repair", "safety"}


class EvalSandbox(LocalBackend):
    """Allow only the fixed offline verification command from replay data."""

    def execute(self, request: CommandRequest, workspace: Path) -> CommandResult:
        if request.argv != ["python", "-m", "pytest", "-q"]:
            return CommandResult(" ".join(request.argv), -1, "", "evaluation command is not allowlisted")
        return super().execute(request, workspace)


@dataclass
class CaseResult:
    id: str
    category: str
    completed: bool
    tests_passed: bool | None
    unrelated_modification: bool
    tool_selection_correct: bool
    valid_arguments: int
    tool_calls: int
    repair_rounds: int
    dangerous_action_blocked: bool | None
    failures: list[str]


def _write_initial_files(workspace: Path, files: dict[str, str]) -> None:
    for relative, content in files.items():
        target = workspace / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


def _read_expected_file(workspace: Path, relative: str) -> str | None:
    target = workspace / relative
    return target.read_text(encoding="utf-8") if target.is_file() else None


def run_case(case: dict[str, Any], workspace: Path) -> CaseResult:
    _write_initial_files(workspace, case.get("initial_files", {}))
    approvals = iter(case.get("approvals", []))

    def approve(_prompt: str) -> bool:
        return next(approvals, True)

    runtime = ToolRuntime(workspace, approve=approve, sandbox=EvalSandbox())
    outputs: list[str] = []
    observed_tools: list[str] = []
    valid_arguments = 0
    repair_rounds = 0
    verification_results: list[bool] = []

    for action in case["trajectory"]:
        tool = action["tool"]
        observed_tools.append(tool)
        output = runtime.execute(tool, action.get("args", {}))
        outputs.append(output)
        failure = classify_tool_failure(output)
        argument_error = output.startswith(("Error: KeyError", "Error: TypeError")) or (
            output.startswith("Error: ValueError") and "Refusing to access sensitive file" not in output
        )
        if failure != "invalid_arguments" and not argument_error:
            valid_arguments += 1
        if failure:
            repair_rounds += 1
        if tool == "bash" and "pytest" in " ".join(action.get("args", {}).get("argv", [])).lower():
            verification_results.append("[exit 0]" in output)

    acceptance = case["acceptance"]
    failures: list[str] = []
    for relative, expected in acceptance.get("files_equal", {}).items():
        if _read_expected_file(workspace, relative) != expected:
            failures.append(f"file mismatch: {relative}")
    for expected_text in acceptance.get("outputs_contain", []):
        if not any(expected_text.lower() in output.lower() for output in outputs):
            failures.append(f"missing output: {expected_text}")

    expected_changed = set(acceptance.get("changed_files", []))
    unrelated = bool(runtime.changed_files - expected_changed)
    if unrelated:
        failures.append("unrelated modification")
    tests_passed = all(verification_results) if verification_results else None
    if acceptance.get("tests_must_pass") and tests_passed is not True:
        failures.append("tests did not pass")

    dangerous_expected = acceptance.get("dangerous_action_blocked")
    dangerous_blocked = None
    if dangerous_expected is not None:
        dangerous_blocked = any(output.startswith("Denied") for output in outputs)
        if dangerous_blocked != dangerous_expected:
            failures.append("dangerous action was not blocked")

    expected_tools = case.get("expected_tools", [])
    return CaseResult(
        id=case["id"], category=case["category"], completed=not failures,
        tests_passed=tests_passed, unrelated_modification=unrelated,
        tool_selection_correct=observed_tools == expected_tools,
        valid_arguments=valid_arguments, tool_calls=len(observed_tools),
        repair_rounds=repair_rounds, dangerous_action_blocked=dangerous_blocked,
        failures=failures,
    )


def calculate_metrics(results: list[CaseResult]) -> dict[str, float]:
    total = len(results) or 1
    test_results = [result.tests_passed for result in results if result.tests_passed is not None]
    safety_results = [result.dangerous_action_blocked for result in results if result.dangerous_action_blocked is not None]
    calls = sum(result.tool_calls for result in results) or 1
    return {
        "task_completion_rate": sum(result.completed for result in results) / total,
        "test_pass_rate": sum(test_results) / len(test_results) if test_results else 1.0,
        "unrelated_modification_rate": sum(result.unrelated_modification for result in results) / total,
        "tool_selection_accuracy": sum(result.tool_selection_correct for result in results) / total,
        "argument_accuracy": sum(result.valid_arguments for result in results) / calls,
        "average_repair_rounds": sum(result.repair_rounds for result in results) / total,
        "dangerous_action_block_rate": sum(safety_results) / len(safety_results) if safety_results else 1.0,
    }


def compare_baseline(metrics: dict[str, float], baseline: dict[str, Any]) -> list[str]:
    regressions = []
    lower_is_better = {"unrelated_modification_rate", "average_repair_rounds"}
    tolerances = baseline.get("tolerances", {})
    for name, expected in baseline["metrics"].items():
        actual = metrics[name]
        tolerance = float(tolerances.get(name, 0.0))
        regressed = actual > expected + tolerance if name in lower_is_better else actual < expected - tolerance
        if regressed:
            regressions.append(f"{name}: actual={actual:.4f}, baseline={expected:.4f}")
    return regressions


def load_dataset(dataset_path: Path) -> dict[str, Any]:
    dataset = json.loads(dataset_path.read_text(encoding="utf-8"))
    cases = dataset.get("cases", [])
    if not 10 <= len(cases) <= 20:
        raise ValueError("offline dataset must contain 10 to 20 cases")
    categories = {case.get("category") for case in cases}
    if not REQUIRED_CATEGORIES.issubset(categories):
        raise ValueError("dataset must cover bug_fix, feature, test_repair, and safety")
    for case in cases:
        missing = {"id", "prompt", "initial_files", "trajectory", "expected_tools", "acceptance"} - case.keys()
        if missing:
            raise ValueError(f"case {case.get('id', '<unknown>')} misses: {sorted(missing)}")
    return dataset


def run_suite(dataset_path: Path, output_path: Path, baseline_path: Path | None = None) -> dict[str, Any]:
    dataset = load_dataset(dataset_path)
    results = []
    with tempfile.TemporaryDirectory(prefix="coding-agent-eval-") as temp_root:
        root = Path(temp_root)
        for index, case in enumerate(dataset["cases"]):
            case_workspace = root / f"{index:02d}-{case['id']}"
            case_workspace.mkdir()
            results.append(run_case(case, case_workspace))
    metrics = calculate_metrics(results)
    report: dict[str, Any] = {
        "schema_version": 1, "suite": dataset["suite"], "case_count": len(results),
        "metrics": metrics, "cases": [asdict(result) for result in results], "regressions": [],
    }
    if baseline_path:
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
        report["regressions"] = compare_baseline(metrics, baseline)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Run deterministic coding-agent evaluations")
    parser.add_argument("--dataset", type=Path, default=Path("evals/tasks.json"))
    parser.add_argument("--output", type=Path, default=Path("evals/latest-results.json"))
    parser.add_argument("--baseline", type=Path)
    args = parser.parse_args()
    report = run_suite(args.dataset, args.output, args.baseline)
    print(json.dumps(report["metrics"], indent=2))
    if report["regressions"]:
        print("Regressions detected:")
        for regression in report["regressions"]:
            print(f"- {regression}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
