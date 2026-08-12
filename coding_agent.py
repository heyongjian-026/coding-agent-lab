"""A small, usable CLI coding-agent harness extracted from lesson s20.

The module deliberately keeps one file so it is easy to study and move into a
new repository.  It supports a single agent, workspace-safe file tools,
approval gates, command execution, retry/compaction, verification, and logs.
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import shlex
import subprocess
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable


MAX_OUTPUT_CHARS = 20_000
SENSITIVE_FILENAMES = {".env", ".env.local", ".env.production"}
DENY_PATTERNS = (
    "rm -rf /",
    "sudo reboot",
    "sudo shutdown",
    "shutdown",
    "reboot",
    "mkfs",
    "dd if=",
    "> /dev/sda",
    "format c:",
)
ASK_PATTERNS = (
    "rm ",
    "del ",
    "remove-item",
    "rmdir",
    "chmod",
    "chown",
    "git clean",
    "git reset",
    "git checkout",
    "git switch",
    "git commit",
    "git push",
    "pip install",
    "npm install",
    "npm run",
    "npm test",
    "pytest",
    "powershell",
    "pwsh",
    "cmd /c",
)


def resolve_workspace_path(path: str, workspace: Path) -> Path:
    """Resolve *path* and reject paths (including symlinks) outside workspace."""
    root = workspace.resolve()
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = root / candidate
    candidate = candidate.resolve()
    if not candidate.is_relative_to(root):
        raise ValueError(f"Path is outside workspace: {path}")
    return candidate


def _reject_sensitive_file(path: Path) -> None:
    if path.name.lower() in SENSITIVE_FILENAMES or path.name.lower().startswith(".env."):
        raise ValueError(f"Refusing to access sensitive file: {path.name}")


def read_file(
    path: str, workspace: Path, offset: int = 0, limit: int = 400
) -> str:
    target = resolve_workspace_path(path, workspace)
    _reject_sensitive_file(target)
    if offset < 0 or limit <= 0:
        raise ValueError("offset must be >= 0 and limit must be > 0")
    if not target.is_file():
        raise ValueError(f"Not a file: {path}")
    lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(lines[offset : offset + limit])


def search_files(pattern: str, workspace: Path) -> str:
    root = workspace.resolve()
    matches = []
    for match in root.glob(pattern):
        resolved = match.resolve()
        if resolved.is_file() and resolved.is_relative_to(root):
            matches.append(resolved.relative_to(root).as_posix())
    return "\n".join(sorted(matches)) or "(no matches)"


def _patched_content(path: str, old_text: str, new_text: str, workspace: Path) -> tuple[Path, str, str]:
    target = resolve_workspace_path(path, workspace)
    _reject_sensitive_file(target)
    if not target.exists():
        if old_text:
            raise ValueError("Cannot replace text in a file that does not exist")
        return target, "", new_text
    if not target.is_file():
        raise ValueError(f"Not a file: {path}")
    original = target.read_text(encoding="utf-8", errors="replace")
    if not old_text:
        raise ValueError("old_text may be empty only when creating a new file")
    if original.count(old_text) != 1:
        raise ValueError("old_text must occur exactly once")
    return target, original, original.replace(old_text, new_text, 1)


def preview_patch(path: str, old_text: str, new_text: str, workspace: Path) -> str:
    target, original, updated = _patched_content(path, old_text, new_text, workspace)
    relative = target.relative_to(workspace.resolve()).as_posix()
    return "".join(
        difflib.unified_diff(
            original.splitlines(keepends=True),
            updated.splitlines(keepends=True),
            fromfile=relative,
            tofile=relative,
        )
    ) or "(no changes)"


def apply_patch(path: str, old_text: str, new_text: str, workspace: Path) -> str:
    target, original, updated = _patched_content(path, old_text, new_text, workspace)
    if original == updated:
        return f"No changes to {path}"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(updated, encoding="utf-8")
    return f"Updated {target.relative_to(workspace.resolve()).as_posix()}"


@dataclass
class CommandResult:
    command: str
    returncode: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False

    def render(self) -> str:
        status = "timed out" if self.timed_out else f"exit {self.returncode}"
        output = "\n".join(part for part in (self.stdout, self.stderr) if part)
        return f"[{status}]\n{output or '(no output)'}"


def classify_command(command: str) -> str:
    normalized = " ".join(command.strip().lower().split())
    if any(pattern in normalized for pattern in DENY_PATTERNS):
        return "deny"
    if any(pattern in normalized for pattern in ASK_PATTERNS):
        return "ask"
    return "allow"


def _split_command(command: str) -> list[str]:
    if not command.strip():
        raise ValueError("command cannot be empty")
    if any(operator in command for operator in ("&&", "||", ";", "|", ">", "<")):
        raise ValueError("shell operators are not supported; run one program at a time")
    return shlex.split(command, posix=os.name != "nt")


def _clip(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    if len(value) > MAX_OUTPUT_CHARS:
        return value[:MAX_OUTPUT_CHARS] + "\n...[output truncated]"
    return value


def run_command(command: str, workspace: Path, timeout: int = 60) -> CommandResult:
    argv = _split_command(command)
    try:
        result = subprocess.run(
            argv,
            cwd=workspace.resolve(),
            capture_output=True,
            text=True,
            errors="replace",
            stdin=subprocess.DEVNULL,
            timeout=timeout,
        )
        return CommandResult(
            command, result.returncode, _clip(result.stdout), _clip(result.stderr)
        )
    except subprocess.TimeoutExpired as error:
        return CommandResult(
            command,
            -1,
            _clip(error.output),
            _clip(error.stderr) or f"Timed out after {timeout}s",
            True,
        )
    except (OSError, ValueError) as error:
        return CommandResult(command, -1, "", str(error))


def detect_verification_commands(workspace: Path) -> list[str]:
    commands: list[str] = []
    if any((workspace / name).exists() for name in ("pytest.ini", "pyproject.toml", "setup.cfg")) or (workspace / "tests").is_dir():
        commands.append("python -m pytest -q")
    package_json = workspace / "package.json"
    if package_json.exists():
        try:
            scripts = json.loads(package_json.read_text(encoding="utf-8")).get("scripts", {})
            if "test" in scripts:
                commands.append("npm test -- --runInBand")
            if "build" in scripts:
                commands.append("npm run build")
        except (OSError, json.JSONDecodeError):
            pass
    return commands


def git_summary(workspace: Path) -> str:
    result = run_command("git status --short", workspace, timeout=15)
    return result.stdout if result.returncode == 0 else "(Git status unavailable)"


def _redact(value: Any) -> Any:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    text = re.sub(
        r"(?i)(api[_-]?key|token|password|secret)(\s*[:=]\s*)[^\s,}\"]+",
        r"\1\2[REDACTED]",
        text,
    )
    return text


class SessionLogger:
    def __init__(self, workspace: Path, session_id: str | None = None):
        log_dir = workspace.resolve() / ".coding-agent"
        log_dir.mkdir(exist_ok=True)
        session_id = session_id or datetime.now().strftime("%Y%m%d-%H%M%S")
        self.path = log_dir / f"session-{session_id}.jsonl"

    def log(self, event: str, data: dict) -> None:
        record = {
            "time": datetime.now().isoformat(timespec="seconds"),
            "event": event,
            "data": _redact(data),
        }
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


TOOLS = [
    {
        "name": "search_files",
        "description": "Find workspace files matching a glob pattern.",
        "input_schema": {
            "type": "object",
            "properties": {"pattern": {"type": "string"}},
            "required": ["pattern"],
        },
    },
    {
        "name": "read_file",
        "description": "Read a range of lines from a workspace file.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "offset": {"type": "integer"},
                "limit": {"type": "integer"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "apply_patch",
        "description": "Replace text exactly once; use empty old_text only to create a new file.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old_text": {"type": "string"},
                "new_text": {"type": "string"},
            },
            "required": ["path", "old_text", "new_text"],
        },
    },
    {
        "name": "run_command",
        "description": "Run one program in the workspace; shell operators are unsupported.",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "timeout": {"type": "integer"},
            },
            "required": ["command"],
        },
    },
]


class ToolRuntime:
    def __init__(
        self,
        workspace: Path,
        approve: Callable[[str], bool],
        logger: SessionLogger | None = None,
    ):
        self.workspace = workspace.resolve()
        self.approve = approve
        self.logger = logger or SessionLogger(self.workspace)
        self.changed_files: set[str] = set()
        self.verification: list[str] = []

    def reset_turn(self) -> None:
        """Clear per-request results while keeping the session and logger alive."""
        self.changed_files.clear()
        self.verification.clear()

    def execute(self, name: str, args: dict) -> str:
        self.logger.log("tool_start", {"name": name, "args": args})
        try:
            if name == "search_files":
                output = search_files(args["pattern"], self.workspace)
            elif name == "read_file":
                output = read_file(
                    args["path"],
                    self.workspace,
                    int(args.get("offset", 0)),
                    int(args.get("limit", 400)),
                )
            elif name == "apply_patch":
                diff = preview_patch(
                    args["path"], args["old_text"], args["new_text"], self.workspace
                )
                if not self.approve(f"Apply this patch?\n{diff}"):
                    output = "Denied by user"
                else:
                    output = apply_patch(
                        args["path"], args["old_text"], args["new_text"], self.workspace
                    )
                    self.changed_files.add(args["path"])
            elif name == "run_command":
                command = args["command"]
                permission = classify_command(command)
                if permission == "deny":
                    output = "Denied: command is on the safety deny list"
                elif permission == "ask" and not self.approve(f"Run command? {command}"):
                    output = "Denied by user"
                else:
                    output = run_command(
                        command, self.workspace, int(args.get("timeout", 60))
                    ).render()
            else:
                output = f"Error: unknown tool '{name}'"
            self.logger.log("tool_result", {"name": name, "output": output})
            return output
        except (KeyError, TypeError, ValueError, OSError) as error:
            output = f"Error: {type(error).__name__}: {error}"
            self.logger.log("tool_error", {"name": name, "error": output})
            return output

    def run_verification(self) -> list[str]:
        results = []
        for command in detect_verification_commands(self.workspace):
            if not self.approve(f"Run detected verification command? {command}"):
                results.append(f"{command}: skipped by user")
                continue
            rendered = run_command(command, self.workspace, timeout=120).render()
            results.append(f"{command}\n{rendered}")
        self.verification = results
        return results


@dataclass
class AgentLimits:
    max_rounds: int = 30
    max_tool_calls: int = 80
    max_context_chars: int = 120_000
    max_tokens: int = 8_000


@dataclass
class RecoveryState:
    retries: int = 0
    compacted: bool = False


@dataclass
class AgentReport:
    final_text: str = ""
    changed_files: list[str] = field(default_factory=list)
    verification: list[str] = field(default_factory=list)
    remaining_issues: list[str] = field(default_factory=list)
    stopped_reason: str = "completed"


def _block_value(block: Any, key: str, default: Any = None) -> Any:
    return block.get(key, default) if isinstance(block, dict) else getattr(block, key, default)


def _message_chars(message: dict) -> int:
    return len(json.dumps(message, ensure_ascii=False, default=str))


def compact_messages(messages: list, max_chars: int) -> list:
    if sum(_message_chars(message) for message in messages) <= max_chars:
        return list(messages)
    keep_from = max(1, len(messages) - 2)
    while keep_from > 1:
        candidate = messages[keep_from:]
        if sum(_message_chars(message) for message in candidate) >= max_chars:
            break
        keep_from -= 1
    recent = messages[keep_from:]
    summary = {
        "role": "user",
        "content": "[Context compacted: earlier messages were removed. Continue from the recent tool state.]",
    }
    return [summary] + recent


def call_with_retry(
    fn: Callable[[], Any],
    max_retries: int = 4,
    sleep: Callable[[float], None] = time.sleep,
) -> Any:
    last_error: Exception | None = None
    for attempt in range(max_retries):
        try:
            return fn()
        except Exception as error:
            text = f"{type(error).__name__} {error}".lower()
            if not any(marker in text for marker in ("429", "529", "ratelimit", "overloaded")):
                raise
            last_error = error
            if attempt + 1 < max_retries:
                sleep(min(2**attempt, 8))
    raise RuntimeError(f"Transient API error after {max_retries} attempts: {last_error}")


def _extract_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    return "\n".join(
        str(_block_value(block, "text", ""))
        for block in (content or [])
        if _block_value(block, "type") == "text"
    ).strip()


def build_system_prompt(workspace: Path) -> str:
    return (
        "You are a careful CLI coding agent. "
        f"Your workspace is {workspace.resolve()}. "
        "Inspect relevant files before editing. Use search_files and read_file, "
        "apply minimal patches, then run relevant tests or builds. Never claim "
        "verification succeeded unless a tool result proves it. Respect denied "
        "operations and finish with changed files, verification, and remaining risks."
    )


def agent_loop(
    client: Any,
    messages: list,
    runtime: ToolRuntime,
    model: str,
    limits: AgentLimits | None = None,
) -> AgentReport:
    limits = limits or AgentLimits()
    report = AgentReport()
    tool_calls = 0
    state = RecoveryState()

    for _round in range(limits.max_rounds):
        if sum(_message_chars(message) for message in messages) > limits.max_context_chars:
            messages[:] = compact_messages(messages, limits.max_context_chars)
            state.compacted = True

        def request():
            return client.messages.create(
                model=model,
                system=build_system_prompt(runtime.workspace),
                messages=messages,
                tools=TOOLS,
                max_tokens=limits.max_tokens,
            )

        try:
            response = call_with_retry(request)
        except Exception as error:
            report.stopped_reason = "api_error"
            report.remaining_issues.append(f"API error: {error}")
            break

        messages.append({"role": "assistant", "content": response.content})
        tool_blocks = [
            block
            for block in response.content
            if _block_value(block, "type") == "tool_use"
        ]
        if not tool_blocks:
            report.final_text = _extract_text(response.content)
            report.stopped_reason = "completed"
            break

        results = []
        for block in tool_blocks:
            tool_calls += 1
            if tool_calls > limits.max_tool_calls:
                report.stopped_reason = "tool_limit"
                report.remaining_issues.append("Maximum tool-call limit reached")
                break
            name = _block_value(block, "name", "")
            args = _block_value(block, "input", {})
            if not isinstance(args, dict):
                output = "Error: malformed tool arguments"
            else:
                output = runtime.execute(name, args)
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": _block_value(block, "id", ""),
                    "content": str(output),
                }
            )
        if results:
            messages.append({"role": "user", "content": results})
        if report.stopped_reason == "tool_limit":
            break
    else:
        report.stopped_reason = "round_limit"
        report.remaining_issues.append("Maximum agent-round limit reached")

    report.changed_files = sorted(runtime.changed_files)
    report.verification = list(runtime.verification)
    return report


def _terminal_approve(prompt: str) -> bool:
    print(f"\n{prompt}")
    return input("Allow? [y/N] ").strip().lower() in {"y", "yes"}


def _print_report(report: AgentReport, workspace: Path) -> None:
    if report.final_text:
        print(f"\n{report.final_text}")
    print("\n--- Final report ---")
    print(f"Stopped: {report.stopped_reason}")
    print("Changed files: " + (", ".join(report.changed_files) or "none"))
    print("Verification:")
    for item in report.verification or ["not run"]:
        print(f"  {item}")
    if report.remaining_issues:
        print("Remaining issues: " + "; ".join(report.remaining_issues))
    print("Git status:")
    print(git_summary(workspace))


def run_cli(workspace: Path | None = None) -> int:
    parser = argparse.ArgumentParser(description="Small workspace-safe CLI coding agent")
    parser.add_argument("--workspace", type=Path, default=workspace or Path.cwd())
    parser.add_argument("--model", default=None)
    args = parser.parse_args()

    try:
        from dotenv import load_dotenv

        load_dotenv()
        from anthropic import Anthropic
    except ImportError as error:
        print(f"Missing dependency: {error}")
        return 2

    root = args.workspace.resolve()
    if not root.is_dir():
        print(f"Workspace does not exist: {root}")
        return 2
    model = args.model or os.getenv("MODEL_ID")
    if not model:
        print("Set MODEL_ID in .env or pass --model")
        return 2

    runtime = ToolRuntime(root, approve=_terminal_approve)
    client = Anthropic()
    history: list[dict] = []
    print(f"Coding Agent workspace: {root}")
    print("Enter a coding task; q exits.\n")
    try:
        while True:
            query = input("code >> ").strip()
            if query.lower() in {"", "q", "quit", "exit"}:
                return 0
            runtime.reset_turn()
            history.append({"role": "user", "content": query})
            report = agent_loop(client, history, runtime, model)
            if runtime.changed_files:
                report.verification = runtime.run_verification()
            _print_report(report, root)
    except (EOFError, KeyboardInterrupt):
        print("\nInterrupted safely.")
        return 130


if __name__ == "__main__":
    raise SystemExit(run_cli())
