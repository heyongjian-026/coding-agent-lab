"""A small, usable CLI coding-agent harness extracted from lesson s20.

The module deliberately keeps one file so it is easy to study and move into a
new repository.  It supports a single agent, workspace-safe file tools,
approval gates, command execution, retry/compaction, verification, and logs.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from anthropic import Anthropic

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


# s20-compatible handler name; workspace remains explicit for this standalone CLI.
def run_read(
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


# s20-compatible handler name.
def run_glob(pattern: str, workspace: Path) -> str:
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


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def apply_patch(path: str, old_text: str, new_text: str, workspace: Path,
                expected_original_hash: str | None = None) -> str:
    target = resolve_workspace_path(path, workspace)
    _reject_sensitive_file(target)
    current = target.read_text(encoding="utf-8", errors="replace") if target.exists() else ""
    if expected_original_hash is not None and _content_hash(current) != expected_original_hash:
        raise ValueError("File changed after patch approval; refusing to write")
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


@dataclass
class CommandRequest:
    argv: list[str]
    timeout: int = 60

    def validate(self) -> None:
        if not self.argv or any(not isinstance(item, str) or not item for item in self.argv):
            raise ValueError("argv must be a non-empty array of non-empty strings")
        if self.timeout <= 0 or self.timeout > 600:
            raise ValueError("timeout must be between 1 and 600 seconds")

    def fingerprint(self) -> str:
        payload = json.dumps(asdict(self), ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass
class PolicyDecision:
    action: str
    reason: str
    risks: list[str] = field(default_factory=list)


class CommandPolicy:
    """Deterministic policy boundary; the model never authorizes itself."""

    def __init__(self, workspace: Path):
        self.workspace = workspace.resolve()

    def evaluate(self, request: CommandRequest) -> PolicyDecision:
        request.validate()
        command = " ".join(request.argv).lower()
        if any(pattern in command for pattern in DENY_PATTERNS):
            return PolicyDecision("deny", "command matches the destructive deny list", ["destructive"])
        outside = [arg for arg in request.argv[1:] if self._outside_path(arg)]
        if outside:
            return PolicyDecision("deny", f"path argument is outside workspace: {outside[0]}", ["path_escape"])
        return PolicyDecision("ask", "arbitrary command execution requires approval", ["code_execution"])

    def _outside_path(self, argument: str) -> bool:
        if argument.startswith("-") or "://" in argument:
            return False
        looks_like_path = argument.startswith((".", "/", "\\")) or "/" in argument or "\\" in argument or re.match(r"^[A-Za-z]:", argument)
        if not looks_like_path:
            return False
        try:
            return not resolve_workspace_path(argument, self.workspace).is_relative_to(self.workspace)
        except (OSError, ValueError):
            return True


@dataclass(frozen=True)
class ApprovalTicket:
    fingerprint: str

    @classmethod
    def create(cls, request: CommandRequest) -> "ApprovalTicket":
        return cls(request.fingerprint())

    def verify(self, request: CommandRequest) -> None:
        if request.fingerprint() != self.fingerprint:
            raise ValueError("Command changed after approval; refusing to execute")


def classify_command(command: str) -> str:
    normalized = " ".join(command.strip().lower().split())
    if any(pattern in normalized for pattern in DENY_PATTERNS):
        return "deny"
    return "ask"


def classify_tool_failure(output: str) -> str | None:
    """Classify a rendered tool failure for recovery and circuit breaking."""
    lowered = output.lower()
    if lowered.startswith("denied"):
        return "permission"
    if "timed out" in lowered:
        return "timeout"
    if lowered.startswith("error: keyerror") or "malformed tool arguments" in lowered:
        return "invalid_arguments"
    if lowered.startswith(("error:", "error[")) or lowered.startswith("[exit -1]"):
        return "permanent"
    match = re.match(r"\[exit (-?\d+)\]", lowered)
    if match and match.group(1) != "0":
        return "command_failed"
    return None


@dataclass(frozen=True)
class RecoveryDecision:
    action: str
    suggestion: str


def recovery_decision(failure: str) -> RecoveryDecision:
    decisions = {
        "permission": RecoveryDecision("ask_human", "Request different scope or user approval."),
        "timeout": RecoveryDecision("retry", "Retry once with a justified timeout or a smaller command."),
        "invalid_arguments": RecoveryDecision("correct_arguments", "Correct the structured tool arguments."),
        "command_failed": RecoveryDecision("alternative", "Inspect stderr and choose a different fix or command."),
        "permanent": RecoveryDecision("stop", "Stop this path and report the blocking error."),
    }
    return decisions.get(failure, RecoveryDecision("stop", "Stop and request human review."))


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


# s20-compatible handler name with a structured result and stricter execution.
def run_bash(command: str, workspace: Path, timeout: int = 60) -> CommandResult:
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


class SandboxBackend:
    def execute(self, request: CommandRequest, workspace: Path) -> CommandResult:
        raise NotImplementedError


class LocalBackend(SandboxBackend):
    """Compatibility backend for tests; not a security boundary."""

    def execute(self, request: CommandRequest, workspace: Path) -> CommandResult:
        request.validate()
        return run_bash(shlex.join(request.argv), workspace, request.timeout)


class DockerBackend(SandboxBackend):
    """Run commands in a disposable, network-disabled Linux container."""

    def __init__(self, image: str = "python:3.10-slim", docker_cli: str | None = None):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/:@-]*", image):
            raise ValueError("Invalid Docker image name")
        self.image = image
        self.docker_cli = docker_cli or os.getenv("DOCKER_CLI") or shutil.which("docker") or r"D:\computer\Docker\resources\bin\docker.exe"

    def execute(self, request: CommandRequest, workspace: Path) -> CommandResult:
        request.validate()
        root = workspace.resolve()
        docker_argv = [
            self.docker_cli, "run", "--rm", "--network", "none", "--read-only",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--memory", "512m", "--cpus", "1", "--pids-limit", "128",
            "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m",
            "--mount", f"type=bind,source={root},target=/workspace",
            "--workdir", "/workspace", self.image, *request.argv,
        ]
        try:
            result = subprocess.run(docker_argv, capture_output=True, text=True, errors="replace",
                                    stdin=subprocess.DEVNULL, timeout=request.timeout)
            return CommandResult(shlex.join(request.argv), result.returncode,
                                 _clip(result.stdout), _clip(result.stderr))
        except subprocess.TimeoutExpired as error:
            return CommandResult(shlex.join(request.argv), -1, _clip(error.output),
                                 _clip(error.stderr) or f"Timed out after {request.timeout}s", True)
        except OSError as error:
            return CommandResult(shlex.join(request.argv), -1, "", str(error))


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


def is_verification_command(argv: list[str]) -> bool:
    normalized = " ".join(argv).lower()
    return any(marker in normalized for marker in ("pytest", " test", "test ", "build", "lint", "mypy"))


def git_summary(workspace: Path) -> str:
    result = run_bash("git status --short", workspace, timeout=15)
    return result.stdout if result.returncode == 0 else "(Git status unavailable)"


def _redact(value: Any) -> Any:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    text = re.sub(
        r"(?i)(api[_-]?key|token|password|secret)(\s*[:=]\s*)[^\s,}\"]+",
        r"\1\2[REDACTED]",
        text,
    )
    text = re.sub(r"(?i)bearer\s+[a-z0-9._-]+", "Bearer [REDACTED]", text)
    text = re.sub(r"\b(?:sk|pk)-[A-Za-z0-9_-]{12,}\b", "[REDACTED_KEY]", text)
    return text


def _safe_log_data(event: str, data: dict) -> dict:
    """Keep logs useful without persisting source, patches, or full output."""
    safe = dict(data)
    args = safe.get("args")
    if isinstance(args, dict):
        args = dict(args)
        for key in ("old_text", "new_text", "content"):
            if key in args:
                args[key] = f"[OMITTED {len(str(args[key]))} chars]"
        safe["args"] = args
    if "output" in safe:
        output = str(safe["output"])
        safe["output"] = output[:1000] + ("...[truncated in log]" if len(output) > 1000 else "")
    return safe


INJECTION_PATTERNS = (
    r"ignore (?:all )?(?:previous|prior) instructions",
    r"override (?:the )?(?:system|developer) prompt",
    r"reveal (?:the )?(?:system prompt|api key|secret)",
    r"disable (?:the )?(?:sandbox|safety|approval)",
)


def detect_prompt_injection(text: str) -> list[str]:
    """Basic warning-only detector; authorization remains the real boundary."""
    return [pattern for pattern in INJECTION_PATTERNS if re.search(pattern, text, re.IGNORECASE)]


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
            "data": _redact(_safe_log_data(event, data)),
        }
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


# Keep s20's name for the built-in tool definitions.
BUILTIN_TOOLS = [
    {
        "name": "glob",
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
        "name": "bash",
        "description": "Run one program in the workspace; shell operators are unsupported.",
        "input_schema": {
            "type": "object",
            "properties": {
                "argv": {"type": "array", "items": {"type": "string"}},
                "timeout": {"type": "integer"},
            },
            "required": ["argv"],
        },
    },
]


class ToolRuntime:
    def __init__(
        self,
        workspace: Path,
        approve: Callable[[str], bool],
        logger: SessionLogger | None = None,
        circuit_breaker_threshold: int = 3,
        sandbox: SandboxBackend | None = None,
        verification_failure_limit: int = 2,
    ):
        self.workspace = workspace.resolve()
        self.approve = approve
        self.logger = logger or SessionLogger(self.workspace)
        self.changed_files: set[str] = set()
        self.inspected_files: set[str] = set()
        self.created_files: set[str] = set()
        self.verification: list[str] = []
        self.circuit_breaker_threshold = circuit_breaker_threshold
        self.consecutive_failures: dict[str, tuple[str, int]] = {}
        self.open_circuits: set[str] = set()
        self.policy = CommandPolicy(self.workspace)
        self.sandbox = sandbox or DockerBackend()
        self.verification_failure_limit = verification_failure_limit
        self.command_failures: dict[str, int] = {}
        self.completed_actions: list[str] = []
        self.recovery_suggestions: list[str] = []

    def reset_turn(self) -> None:
        """Clear per-request results while keeping the session and logger alive."""
        self.changed_files.clear()
        self.inspected_files.clear()
        self.created_files.clear()
        self.verification.clear()
        self.consecutive_failures.clear()
        self.open_circuits.clear()
        self.command_failures.clear()
        self.completed_actions.clear()
        self.recovery_suggestions.clear()

    def _record_outcome(self, name: str, output: str) -> None:
        failure = classify_tool_failure(output)
        if failure is None:
            self.consecutive_failures.pop(name, None)
            if name not in self.completed_actions:
                self.completed_actions.append(name)
            return
        if failure == "permission":
            return
        decision = recovery_decision(failure)
        suggestion = f"{name}: {decision.action} — {decision.suggestion}"
        if suggestion not in self.recovery_suggestions:
            self.recovery_suggestions.append(suggestion)
        previous_kind, previous_count = self.consecutive_failures.get(name, ("", 0))
        count = previous_count + 1 if previous_kind == failure else 1
        self.consecutive_failures[name] = (failure, count)
        if count >= self.circuit_breaker_threshold:
            self.open_circuits.add(name)
            self.logger.log("circuit_open", {"name": name, "failure": failure, "count": count})

    def execute(self, name: str, args: dict) -> str:
        self.logger.log("tool_start", {"name": name, "args": args})
        if name in self.open_circuits:
            output = f"Error[circuit_open]: repeated failures disabled tool '{name}' for this turn"
            self.logger.log("tool_result", {"name": name, "output": output})
            return output
        try:
            if name == "glob":
                output = run_glob(args["pattern"], self.workspace)
            elif name == "read_file":
                output = run_read(
                    args["path"],
                    self.workspace,
                    int(args.get("offset", 0)),
                    int(args.get("limit", 400)),
                )
                warnings = detect_prompt_injection(output)
                if warnings:
                    self.logger.log("injection_warning", {"path": args["path"], "matches": warnings})
                    output = "[Security warning: untrusted file contains instruction-like text]\n" + output
                self.inspected_files.add(args["path"])
            elif name == "apply_patch":
                _, original, _ = _patched_content(
                    args["path"], args["old_text"], args["new_text"], self.workspace
                )
                approved_hash = _content_hash(original)
                diff = preview_patch(
                    args["path"], args["old_text"], args["new_text"], self.workspace
                )
                if not self.approve(f"Apply this patch?\n{diff}"):
                    self.logger.log("approval", {"operation": "apply_patch", "allowed": False})
                    output = "Denied by user"
                else:
                    self.logger.log("approval", {"operation": "apply_patch", "allowed": True})
                    output = apply_patch(
                        args["path"], args["old_text"], args["new_text"], self.workspace,
                        expected_original_hash=approved_hash,
                    )
                    self.changed_files.add(args["path"])
                    if not original:
                        self.created_files.add(args["path"])
            elif name == "bash":
                request = CommandRequest(list(args["argv"]), int(args.get("timeout", 60)))
                command_key = json.dumps(request.argv, ensure_ascii=False)
                if self.command_failures.get(command_key, 0) >= self.verification_failure_limit:
                    output = "Error[circuit_open]: repeated command failures reached the repair limit"
                    self.open_circuits.add("bash")
                    self._record_outcome(name, output)
                    self.logger.log("tool_result", {"name": name, "output": output})
                    return output
                decision = self.policy.evaluate(request)
                self.logger.log("policy_decision", asdict(decision))
                if decision.action == "deny":
                    output = f"Denied: {decision.reason}"
                else:
                    ticket = ApprovalTicket.create(request)
                    shown = shlex.join(request.argv)
                    allowed = self.approve(f"Run sandboxed command? {shown}\nRisk: {decision.reason}")
                    self.logger.log("approval", {"operation": "bash", "allowed": allowed,
                                                  "fingerprint": ticket.fingerprint})
                    if not allowed:
                        output = "Denied by user"
                    else:
                        ticket.verify(request)
                        output = self.sandbox.execute(request, self.workspace).render()
                        if is_verification_command(request.argv):
                            self.verification.append(f"{shlex.join(request.argv)}\n{output}")
                        if classify_tool_failure(output) == "command_failed":
                            self.command_failures[command_key] = self.command_failures.get(command_key, 0) + 1
                        else:
                            self.command_failures.pop(command_key, None)
            else:
                output = f"Error: unknown tool '{name}'"
            self._record_outcome(name, output)
            self.logger.log("tool_result", {"name": name, "output": output})
            return output
        except (KeyError, TypeError, ValueError, OSError) as error:
            output = f"Error: {type(error).__name__}: {error}"
            self._record_outcome(name, output)
            self.logger.log("tool_error", {"name": name, "error": output})
            return output

    def run_verification(self) -> list[str]:
        results = []
        for command in detect_verification_commands(self.workspace):
            rendered = self.execute(
                "bash", {"argv": _split_command(command), "timeout": 120}
            )
            results.append(f"{command}\n{rendered}")
        self.verification = results
        return results


@dataclass
class AgentLimits:
    max_rounds: int = 30
    max_tool_calls: int = 80
    max_context_chars: int = 120_000
    max_tokens: int = 8_000
    max_rework_cycles: int = 2
    replan_failure_threshold: int = 2


PLAN_STATUSES = {"pending", "in_progress", "completed", "failed"}


@dataclass
class PlanStep:
    id: int
    title: str
    kind: str
    status: str = "pending"
    attempts: int = 0
    evidence: list[str] = field(default_factory=list)

    def set_status(self, status: str) -> None:
        if status not in PLAN_STATUSES:
            raise ValueError(f"Invalid plan status: {status}")
        self.status = status


@dataclass
class TaskPlan:
    original_request: str
    mode: str
    steps: list[PlanStep]
    replans: int = 0
    events: list[str] = field(default_factory=list)

    @property
    def current_step(self) -> PlanStep | None:
        return next(
            (step for step in self.steps if step.status in {"in_progress", "pending"}),
            None,
        )

    def start_next(self) -> PlanStep | None:
        step = self.current_step
        if step and step.status == "pending":
            step.set_status("in_progress")
        return step

    def step_for_tool(self, tool_name: str) -> PlanStep | None:
        kind = {
            "glob": "inspect",
            "read_file": "inspect",
            "apply_patch": "implement",
            "bash": "verify",
        }.get(tool_name, "execute")
        return next(
            (step for step in self.steps if step.kind == kind and step.status != "completed"),
            self.current_step,
        )

    def record_tool_result(self, tool_name: str, output: str) -> None:
        step = self.step_for_tool(tool_name)
        if step is None:
            return
        step.attempts += 1
        failure = classify_tool_failure(output)
        if failure:
            step.set_status("failed")
            step.evidence.append(f"{tool_name}: {failure}")
            self.events.append(f"step {step.id} failed; ReAct adjustment requested")
            return
        step.set_status("completed")
        step.evidence.append(f"{tool_name}: success")
        self.start_next()

    def replan_remaining(self, reason: str) -> None:
        self.replans += 1
        self.events.append(f"replanned: {reason}")
        for step in self.steps:
            if step.status == "failed":
                step.set_status("in_progress")
                step.title = f"Retry with an alternative approach: {step.title}"
        self.start_next()


CHANGE_INTENT = re.compile(
    r"\b(add|build|change|create|delete|edit|fix|implement|refactor|remove|update)\b|"
    r"增加|修改|修复|实现|重构|删除|创建|完成",
    re.IGNORECASE,
)
COMPLEXITY_MARKERS = re.compile(
    r"\b(and|then|multiple|refactor|architecture|workflow)\b|"
    r"并且|然后|多个|架构|工作流|逐步|重构",
    re.IGNORECASE,
)


def select_execution_mode(request: str) -> str:
    """Choose a direct ReAct loop or deterministic plan-and-execute wrapper."""
    score = int(len(request) >= 100)
    score += int(bool(COMPLEXITY_MARKERS.search(request)))
    score += int(request.count("\n") >= 2)
    score += int(len(re.findall(r"(?:^|\s)\d+[.)、]", request)) >= 2)
    return "plan_and_execute" if score >= 1 else "react"


def create_task_plan(request: str) -> TaskPlan:
    mode = select_execution_mode(request)
    if mode == "react":
        steps = [PlanStep(1, "Inspect and complete the request with ReAct", "execute")]
    else:
        specifications = [("Inspect relevant context", "inspect")]
        if CHANGE_INTENT.search(request):
            specifications.extend(
                [("Implement the requested change", "implement"), ("Run relevant verification", "verify")]
            )
        specifications.append(("Review the result against the request", "review"))
        steps = [PlanStep(index, title, kind) for index, (title, kind) in enumerate(specifications, 1)]
    plan = TaskPlan(request, mode, steps)
    plan.start_next()
    return plan


@dataclass
class CompletionCheck:
    passed: bool
    checks: dict[str, bool]
    issues: list[str] = field(default_factory=list)


class CompletionCriteriaChecker:
    """Independent, evidence-based completion gate outside the model response."""

    def check(self, request: str, plan: TaskPlan, runtime: ToolRuntime) -> CompletionCheck:
        change_requested = bool(CHANGE_INTENT.search(request))
        plan_complete = all(step.status == "completed" for step in plan.steps)
        request_covered = not change_requested or bool(runtime.changed_files)
        scoped_diff = runtime.changed_files.issubset(runtime.inspected_files | runtime.created_files)
        verification_needed = change_requested and bool(detect_verification_commands(runtime.workspace))
        verification_ok = not verification_needed or any(
            classify_tool_failure(item.split("\n", 1)[-1]) is None
            and ("[exit 0]" in item or item.endswith(": success"))
            for item in runtime.verification
        )
        checks = {
            "request_covered": request_covered,
            "no_unrelated_diff": scoped_diff,
            "verification_evidence": verification_ok,
            "plan_complete": plan_complete,
        }
        labels = {
            "request_covered": "No approved file change proves the change request was covered",
            "no_unrelated_diff": "A changed file was neither inspected first nor created by this task",
            "verification_evidence": "Required verification has no successful tool evidence",
            "plan_complete": "One or more planned steps are incomplete",
        }
        issues = [labels[name] for name, passed in checks.items() if not passed]
        return CompletionCheck(not issues, checks, issues)


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
    completed_actions: list[str] = field(default_factory=list)
    recovery_suggestions: list[str] = field(default_factory=list)
    plan: TaskPlan | None = None
    completion_check: CompletionCheck | None = None
    rework_cycles: int = 0
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


# Keep s20's retry helper name; injectable limits/sleep make it testable.
def with_retry(
    fn: Callable[[], Any],
    state: RecoveryState,
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
            state.retries += 1
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


def build_system_prompt(workspace: Path, plan: TaskPlan | None = None) -> str:
    prompt = (
        "You are a careful CLI coding agent. "
        f"Your workspace is {workspace.resolve()}. "
        "Inspect relevant files before editing. Use glob and read_file, "
        "apply minimal patches, then run relevant tests or builds. Never claim "
        "verification succeeded unless a tool result proves it. Respect denied "
        "operations and finish with changed files, verification, and remaining risks."
    )
    if plan is not None:
        steps = "; ".join(f"{step.id}. {step.title} [{step.status}]" for step in plan.steps)
        prompt += f" Execution mode: {plan.mode}. Current plan: {steps}."
    return prompt


def _latest_user_request(messages: list) -> str:
    for message in reversed(messages):
        if message.get("role") == "user" and isinstance(message.get("content"), str):
            return message["content"]
    return ""


def _complete_terminal_step(plan: TaskPlan, runtime: ToolRuntime) -> None:
    current = plan.current_step
    if current and current.kind == "verify" and not detect_verification_commands(runtime.workspace):
        current.set_status("completed")
        current.evidence.append("no configured verification command")
        current = plan.start_next()
    if current and current.kind in {"review", "execute"}:
        current.set_status("completed")
        current.evidence.append("model returned final review")


def _prepare_rework(plan: TaskPlan, issues: list[str]) -> None:
    plan.events.append("completion check failed: " + "; ".join(issues))
    requested_kinds = []
    if any("change request" in issue for issue in issues):
        requested_kinds.extend(["implement", "execute"])
    if any("verification" in issue for issue in issues):
        requested_kinds.extend(["verify", "execute"])
    if any("changed file" in issue for issue in issues):
        requested_kinds.extend(["review", "inspect"])
    for kind in requested_kinds:
        step = next((item for item in plan.steps if item.kind == kind), None)
        if step:
            step.set_status("in_progress")
            return
    step = next((item for item in plan.steps if item.status != "completed"), None)
    if step:
        step.set_status("in_progress")


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
    request_text = _latest_user_request(messages)
    plan = create_task_plan(request_text)
    checker = CompletionCriteriaChecker()
    report.plan = plan

    for _round in range(limits.max_rounds):
        if sum(_message_chars(message) for message in messages) > limits.max_context_chars:
            messages[:] = compact_messages(messages, limits.max_context_chars)
            state.compacted = True

        def request():
            return client.messages.create(
                model=model,
                system=build_system_prompt(runtime.workspace, plan),
                messages=messages,
                tools=BUILTIN_TOOLS,
                max_tokens=limits.max_tokens,
            )

        try:
            response = with_retry(request, state)
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
            _complete_terminal_step(plan, runtime)
            report.completion_check = checker.check(request_text, plan, runtime)
            if report.completion_check.passed:
                report.stopped_reason = "completed"
                break
            if report.rework_cycles >= limits.max_rework_cycles:
                report.stopped_reason = "rework_limit"
                report.remaining_issues.extend(report.completion_check.issues)
                break
            report.rework_cycles += 1
            _prepare_rework(plan, report.completion_check.issues)
            plan.replan_remaining("completion criteria failed")
            messages.append(
                {
                    "role": "user",
                    "content": "Completion check failed. Continue working and address: "
                    + "; ".join(report.completion_check.issues),
                }
            )
            continue

        results = []
        for block in tool_blocks:
            if tool_calls >= limits.max_tool_calls:
                report.stopped_reason = "tool_limit"
                if "Maximum tool-call limit reached" not in report.remaining_issues:
                    report.remaining_issues.append("Maximum tool-call limit reached")
                output = "Error[tool_limit]: tool call was not executed"
            else:
                tool_calls += 1
                name = _block_value(block, "name", "")
                args = _block_value(block, "input", {})
                if not isinstance(args, dict):
                    output = "Error: malformed tool arguments"
                else:
                    output = runtime.execute(name, args)
                plan.record_tool_result(name, str(output))
                failed_step = plan.step_for_tool(name)
                if (
                    failed_step
                    and failed_step.status == "failed"
                    and failed_step.attempts >= limits.replan_failure_threshold
                ):
                    plan.replan_remaining(f"repeated {name} failure")
                elif failed_step and failed_step.status == "failed":
                    failed_step.set_status("in_progress")
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
    report.completed_actions = list(runtime.completed_actions)
    report.recovery_suggestions = list(runtime.recovery_suggestions)
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
    print("Completed actions: " + (", ".join(report.completed_actions) or "none"))
    if report.plan:
        print(f"Plan ({report.plan.mode}, replans={report.plan.replans}):")
        for step in report.plan.steps:
            print(f"  {step.id}. [{step.status}] {step.title}")
    print("Verification:")
    for item in report.verification or ["not run"]:
        print(f"  {item}")
    if report.remaining_issues:
        print("Remaining issues: " + "; ".join(report.remaining_issues))
    if report.recovery_suggestions:
        print("Recovery suggestions: " + "; ".join(report.recovery_suggestions))
    if report.completion_check:
        status = "passed" if report.completion_check.passed else "failed"
        print(f"Completion check: {status}")
    print("Git status:")
    print(git_summary(workspace))


def _create_client(client_class: Any) -> Any:
    """Create the SDK client from explicit project configuration.

    Passing both values prevents an unrelated ANTHROPIC_AUTH_TOKEN injected by
    the parent terminal or IDE from silently overriding this project's key.
    """
    return client_class(
        api_key=os.getenv("ANTHROPIC_API_KEY"),
        base_url=os.getenv("ANTHROPIC_BASE_URL"),
    )


def run_cli(workspace: Path | None = None) -> int:
    parser = argparse.ArgumentParser(description="Small workspace-safe CLI coding agent")
    parser.add_argument("--workspace", type=Path, default=workspace or Path.cwd())
    parser.add_argument("--model", default=None)
    args = parser.parse_args()

    try:
        from dotenv import load_dotenv

        load_dotenv(Path(__file__).with_name(".env"), override=True)
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
    client = _create_client(Anthropic)
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
