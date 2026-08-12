import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

try:
    # Standalone repository layout.
    import coding_agent as ca
except ModuleNotFoundError:
    # Layout while this project still lives under learn-claude-code/hyj.
    from hyj import coding_agent as ca


def test_resolve_workspace_path_rejects_escape(tmp_path):
    with pytest.raises(ValueError, match="outside workspace"):
        ca.resolve_workspace_path("../secret.txt", tmp_path)


def test_read_search_and_exact_patch(tmp_path):
    source = tmp_path / "src" / "app.py"
    source.parent.mkdir()
    source.write_text("one\ntwo\nthree\n", encoding="utf-8")

    assert ca.read_file("src/app.py", tmp_path, offset=1, limit=1) == "two"
    assert ca.search_files("**/*.py", tmp_path) == "src/app.py"
    assert "Updated src/app.py" in ca.apply_patch(
        "src/app.py", "two", "TWO", tmp_path
    )
    assert "TWO" in source.read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="exactly once"):
        ca.apply_patch("src/app.py", "missing", "x", tmp_path)


def test_empty_old_text_creates_new_file(tmp_path):
    result = ca.apply_patch("new/module.py", "", "value = 1\n", tmp_path)
    assert result == "Updated new/module.py"
    assert (tmp_path / "new" / "module.py").read_text(encoding="utf-8") == "value = 1\n"


@pytest.mark.parametrize("command", ["sudo reboot", "rm -rf /", "mkfs /dev/sda"])
def test_classify_command_denies_dangerous_commands(command):
    assert ca.classify_command(command) == "deny"


def test_classify_command_asks_for_sensitive_commands():
    assert ca.classify_command("rm old.txt") == "ask"
    # Tests execute repository code (including plugins), so they need approval.
    assert ca.classify_command("python -m pytest -q") == "ask"


def test_preview_patch_is_unified_diff(tmp_path):
    path = tmp_path / "a.py"
    path.write_text("x = 1\n", encoding="utf-8")
    diff = ca.preview_patch("a.py", "1", "2", tmp_path)
    assert "--- a.py" in diff
    assert "+x = 2" in diff


def test_run_command_returns_timeout(monkeypatch, tmp_path):
    def expire(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"], output="partial")

    monkeypatch.setattr(subprocess, "run", expire)
    result = ca.run_command("python slow.py", tmp_path, timeout=1)
    assert result.timed_out is True
    assert result.returncode == -1


def test_detect_verification_commands(tmp_path):
    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (tmp_path / "package.json").write_text(
        json.dumps({"scripts": {"test": "vitest", "build": "vite build"}}),
        encoding="utf-8",
    )
    commands = ca.detect_verification_commands(tmp_path)
    assert "python -m pytest -q" in commands
    assert "npm test -- --runInBand" in commands
    assert "npm run build" in commands


def test_runtime_requires_patch_approval_and_logs(tmp_path):
    path = tmp_path / "a.py"
    path.write_text("old\n", encoding="utf-8")
    logger = ca.SessionLogger(tmp_path, session_id="test")
    runtime = ca.ToolRuntime(tmp_path, approve=lambda prompt: False, logger=logger)

    result = runtime.execute(
        "apply_patch", {"path": "a.py", "old_text": "old", "new_text": "new"}
    )
    assert result == "Denied by user"
    assert path.read_text(encoding="utf-8") == "old\n"
    events = [json.loads(line) for line in logger.path.read_text().splitlines()]
    assert [event["event"] for event in events] == ["tool_start", "tool_result"]


def test_runtime_rejects_unknown_tool(tmp_path):
    runtime = ca.ToolRuntime(tmp_path, approve=lambda prompt: True)
    assert runtime.execute("unknown", {}) == "Error: unknown tool 'unknown'"


def test_runtime_reset_turn_clears_transient_results(tmp_path):
    runtime = ca.ToolRuntime(tmp_path, approve=lambda prompt: True)
    runtime.changed_files.add("old.py")
    runtime.verification.append("old test")
    runtime.reset_turn()
    assert runtime.changed_files == set()
    assert runtime.verification == []


def test_tool_schemas_have_required_fields():
    names = {tool["name"] for tool in ca.TOOLS}
    assert names == {"search_files", "read_file", "apply_patch", "run_command"}
    for tool in ca.TOOLS:
        assert tool["description"]
        assert tool["input_schema"]["type"] == "object"
        assert isinstance(tool["input_schema"]["required"], list)


def test_compaction_preserves_recent_tool_pair():
    messages = [
        {"role": "user", "content": "x" * 200},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "1"}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "1"}]},
    ]
    compacted = ca.compact_messages(messages, max_chars=50)
    assert compacted[-2:] == messages[-2:]
    assert "compacted" in compacted[0]["content"].lower()


def test_call_with_retry_retries_transient_error():
    attempts = []

    def operation():
        attempts.append(1)
        if len(attempts) < 3:
            raise RuntimeError("429 rate limit")
        return "ok"

    assert ca.call_with_retry(operation, max_retries=3, sleep=lambda _: None) == "ok"
    assert len(attempts) == 3


class FakeMessages:
    def __init__(self, responses):
        self.responses = iter(responses)

    def create(self, **kwargs):
        return next(self.responses)


def test_agent_loop_executes_tool_then_returns_final_text(tmp_path):
    tool = SimpleNamespace(
        type="tool_use", id="call-1", name="read_file", input={"path": "a.py"}
    )
    text = SimpleNamespace(type="text", text="Finished")
    responses = [
        SimpleNamespace(content=[tool], stop_reason="tool_use"),
        SimpleNamespace(content=[text], stop_reason="end_turn"),
    ]
    (tmp_path / "a.py").write_text("print('ok')\n", encoding="utf-8")
    runtime = ca.ToolRuntime(tmp_path, approve=lambda prompt: True)
    client = SimpleNamespace(messages=FakeMessages(responses))
    history = [{"role": "user", "content": "read it"}]

    report = ca.agent_loop(
        client, history, runtime, "fake-model", ca.AgentLimits(max_rounds=3)
    )

    assert report.final_text == "Finished"
    assert history[-2]["role"] == "user"
    assert history[-2]["content"][0]["type"] == "tool_result"


def test_build_system_prompt_contains_workspace(tmp_path):
    prompt = ca.build_system_prompt(tmp_path)
    assert str(tmp_path.resolve()) in prompt
    assert "inspect" in prompt.lower()
