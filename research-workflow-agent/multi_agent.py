"""Role-based, persistent multi-Agent coordination for research workflows."""

from __future__ import annotations

import json
import re
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


@dataclass(frozen=True)
class RoleSpec:
    name: str
    purpose: str
    allowed_tools: frozenset[str]
    input_fields: tuple[str, ...]
    output_fields: tuple[str, ...]


ROLE_SPECS = {
    "planner": RoleSpec(
        "planner", "Break the research goal into ordered, verifiable tasks.",
        frozenset({"create_task", "list_tasks", "recall", "save_working"}),
        ("research_question",), ("plan", "success_criteria"),
    ),
    "researcher": RoleSpec(
        "researcher", "Retrieve sources and produce evidence, not unsupported conclusions.",
        frozenset({"search_knowledge", "add_document", "recall", "add_evidence",
                   "update_progress"}),
        ("research_question", "plan"), ("evidence", "confidence"),
    ),
    "writer": RoleSpec(
        "writer", "Synthesize a draft whose claims point to supplied evidence.",
        frozenset({"search_knowledge", "recall", "list_tasks"}),
        ("research_question", "evidence"), ("draft", "citations"),
    ),
    "reviewer": RoleSpec(
        "reviewer", "Check coverage, evidence and unsupported claims before approval.",
        frozenset({"search_knowledge", "recall"}),
        ("draft", "evidence"), ("approved", "issues", "final"),
    ),
}

ROLE_ALIASES = {"文献分析": "researcher", "paper-reader": "researcher",
                "research": "researcher", "planning": "planner",
                "writing": "writer", "review": "reviewer"}


@dataclass
class Evidence:
    content: str
    source: str
    confidence: float


@dataclass
class AgentResult:
    agent: str
    role: str
    content: str
    evidence: list[Evidence] = field(default_factory=list)
    confidence: float = 0.0
    status: str = "completed"
    payload: dict[str, Any] = field(default_factory=dict)


def parse_role_result(agent: str, role: str, text: str) -> AgentResult:
    """Validate the JSON output contract used by the four-stage workflow."""
    spec = ROLE_SPECS[role]
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{role} must return a JSON object") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{role} must return a JSON object")
    missing = [field for field in spec.output_fields if field not in payload]
    if missing:
        raise ValueError(f"{role} result is missing fields: {', '.join(missing)}")
    evidence = [Evidence(str(item.get("content", "")), str(item.get("source", "")),
                         float(item.get("confidence", 0.0)))
                for item in payload.get("evidence", []) if isinstance(item, dict)]
    return AgentResult(agent, role, text, evidence,
                       float(payload.get("confidence", 0.0)), payload=payload)


@dataclass
class AgentRecord:
    name: str
    role: str
    prompt: str
    task_id: str
    status: str = "pending"
    attempts: int = 1
    created_at: str = field(default_factory=_now)
    started_at: str | None = None
    finished_at: str | None = None
    result: str = ""
    error: str = ""


@dataclass
class WorkflowTask:
    id: str
    role: str
    status: str = "pending"
    depends_on: list[str] = field(default_factory=list)
    result: str = ""


@dataclass
class WorkflowState:
    id: str
    research_question: str
    status: str
    tasks: list[WorkflowTask]
    results: dict[str, str] = field(default_factory=dict)
    updated_at: str = field(default_factory=_now)


@dataclass
class Conclusion:
    id: str
    question: str
    claim: str
    agent: str
    evidence: list[Evidence]
    confidence: float
    status: str = "pending"
    created_at: str = field(default_factory=_now)


@dataclass
class ArbitrationDecision:
    id: str
    question: str
    conclusion_ids: list[str]
    selected_id: str | None
    status: str
    reason: str
    approval_id: str | None = None


class WorkflowStore:
    """Durable Agent identities, workflow state and append-only events."""

    def __init__(self, data_dir: Path):
        self.agents_path = data_dir / "agents.json"
        self.workflows_path = data_dir / "workflows.json"
        self.events_path = data_dir / "workflow_events.jsonl"
        self.snapshots_dir = data_dir / "workflow_snapshots"
        self._lock = threading.RLock()

    def save_agent(self, record: AgentRecord) -> None:
        with self._lock:
            records = self._read(self.agents_path, {})
            records[record.name] = asdict(record)
            _write(self.agents_path, records)

    def load_agent(self, name: str) -> AgentRecord | None:
        item = self._read(self.agents_path, {}).get(name)
        return AgentRecord(**item) if item else None

    def save_workflow(self, state: WorkflowState) -> None:
        with self._lock:
            workflows = self._read(self.workflows_path, {})
            workflows[state.id] = asdict(state)
            _write(self.workflows_path, workflows)

    def load_workflow(self, workflow_id: str) -> WorkflowState | None:
        item = self._read(self.workflows_path, {}).get(workflow_id)
        if not item:
            return None
        item["tasks"] = [WorkflowTask(**task) for task in item["tasks"]]
        return WorkflowState(**item)

    def event(self, event_type: str, payload: dict[str, Any]) -> None:
        record = {"id": _id("evt"), "time": _now(), "type": event_type,
                  "payload": payload}
        with self._lock:
            self.events_path.parent.mkdir(parents=True, exist_ok=True)
            with self.events_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    def snapshot(self, state: WorkflowState, label: str) -> str:
        checkpoint_id = _id("checkpoint")
        payload = {"id": checkpoint_id, "label": label, "created_at": _now(),
                   "state": asdict(state)}
        _write(self.snapshots_dir / state.id / f"{checkpoint_id}.json", payload)
        self.event("checkpoint_saved", {"workflow": state.id,
                                        "checkpoint": checkpoint_id, "label": label})
        return checkpoint_id

    def restore(self, workflow_id: str, checkpoint_id: str) -> WorkflowState:
        self._validate_id(workflow_id, "workflow")
        self._validate_id(checkpoint_id, "checkpoint")
        path = self.snapshots_dir / workflow_id / f"{checkpoint_id}.json"
        item = self._read(path, None)
        if not item or item.get("state", {}).get("id") != workflow_id:
            raise KeyError(f"Checkpoint '{checkpoint_id}' not found")
        state = item["state"]
        state["tasks"] = [WorkflowTask(**task) for task in state["tasks"]]
        restored = WorkflowState(**state)
        self.save_workflow(restored)
        self.event("checkpoint_restored", {"workflow": workflow_id,
                                           "checkpoint": checkpoint_id})
        return restored

    def list_snapshots(self, workflow_id: str) -> list[dict]:
        self._validate_id(workflow_id, "workflow")
        directory = self.snapshots_dir / workflow_id
        return [self._read(path, {}) for path in sorted(directory.glob("checkpoint_*.json"))]

    @staticmethod
    def _validate_id(value: str, prefix: str) -> None:
        if not re.fullmatch(rf"{prefix}_[0-9a-f]{{32}}", value):
            raise ValueError(f"Invalid {prefix} id")

    @staticmethod
    def _read(path: Path, default: Any) -> Any:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


class ConflictArbitrator:
    """Evidence-first conclusion conflict detection and Lead/human arbitration."""

    def __init__(self, data_dir: Path, request_human: Callable[[dict], str] | None = None,
                 human_approved: Callable[[str], bool] | None = None,
                 automatic_margin: float = 0.15):
        self.conclusions_path = data_dir / "conclusions.json"
        self.decisions_path = data_dir / "arbitration_decisions.json"
        self.request_human = request_human
        self.human_approved = human_approved
        self.automatic_margin = automatic_margin
        self._lock = threading.RLock()

    def submit(self, question: str, claim: str, agent: str,
               evidence: list[dict], confidence: float) -> Conclusion:
        if not evidence or any(not item.get("source") for item in evidence):
            raise ValueError("A conclusion requires sourced evidence")
        confidence = float(confidence)
        if not 0.0 <= confidence <= 1.0:
            raise ValueError("confidence must be between 0 and 1")
        parsed = [Evidence(str(item.get("content", "")), str(item["source"]),
                           float(item.get("confidence", confidence))) for item in evidence]
        conclusion = Conclusion(_id("conclusion"), question, claim, agent,
                                parsed, confidence)
        with self._lock:
            items = self._read(self.conclusions_path)
            items[conclusion.id] = asdict(conclusion)
            _write(self.conclusions_path, items)
        return conclusion

    def conflicts(self, question: str) -> list[Conclusion]:
        normalized = self._normalize(question)
        matches = [self._conclusion(item) for item in self._read(self.conclusions_path).values()
                   if self._normalize(item["question"]) == normalized]
        return matches if len({self._normalize(item.claim) for item in matches}) > 1 else []

    def arbitrate(self, conclusion_ids: list[str]) -> ArbitrationDecision:
        items = self._read(self.conclusions_path)
        conclusions = [self._conclusion(items[item_id]) for item_id in conclusion_ids
                       if item_id in items]
        if len(conclusions) != len(set(conclusion_ids)) or len(conclusions) < 2:
            raise ValueError("At least two existing conclusions are required")
        if len({self._normalize(item.question) for item in conclusions}) != 1:
            raise ValueError("Conclusions must answer the same question")
        ranked = sorted(conclusions, key=lambda item: (item.confidence, len(item.evidence)),
                        reverse=True)
        margin = ranked[0].confidence - ranked[1].confidence
        status = "approved" if margin >= self.automatic_margin else "human_required"
        selected_id = ranked[0].id if status == "approved" else None
        reason = (f"Lead selected higher-confidence evidence (margin={margin:.2f})"
                  if selected_id else f"Confidence margin {margin:.2f} requires human review")
        decision = ArbitrationDecision(_id("decision"), ranked[0].question,
                                       list(conclusion_ids), selected_id, status, reason)
        if status == "human_required" and self.request_human:
            decision.approval_id = self.request_human({"decision_id": decision.id,
                                                       "conclusions": conclusion_ids})
        self._save_decision(decision)
        if selected_id:
            self._apply_decision(decision)
        return decision

    def resolve_human(self, decision_id: str, selected_id: str) -> ArbitrationDecision:
        decisions = self._read(self.decisions_path)
        if decision_id not in decisions or selected_id not in decisions[decision_id]["conclusion_ids"]:
            raise ValueError("Invalid decision or selected conclusion")
        decision = ArbitrationDecision(**decisions[decision_id])
        if (not decision.approval_id or not self.human_approved
                or not self.human_approved(decision.approval_id)):
            raise PermissionError("Human approval is required")
        decision.selected_id, decision.status = selected_id, "approved"
        decision.reason = "Human selected the supported conclusion"
        self._save_decision(decision)
        self._apply_decision(decision)
        return decision

    def get_decision(self, decision_id: str) -> ArbitrationDecision | None:
        item = self._read(self.decisions_path).get(decision_id)
        return ArbitrationDecision(**item) if item else None

    def promote(self, decision_id: str, memory: Any) -> str:
        item = self._read(self.decisions_path).get(decision_id)
        if not item or item["status"] != "approved" or not item.get("selected_id"):
            raise ValueError("Only an approved conclusion can enter long-term memory")
        conclusion = self._read(self.conclusions_path)[item["selected_id"]]
        return memory.remember(conclusion["claim"], "confirmed_conclusion",
                               source="reviewer", confidence=conclusion["confidence"],
                               confirmed=True, reviewed_by="lead")

    def _apply_decision(self, decision: ArbitrationDecision) -> None:
        items = self._read(self.conclusions_path)
        for conclusion_id in decision.conclusion_ids:
            items[conclusion_id]["status"] = ("accepted" if conclusion_id == decision.selected_id
                                               else "rejected")
        _write(self.conclusions_path, items)

    def _save_decision(self, decision: ArbitrationDecision) -> None:
        decisions = self._read(self.decisions_path)
        decisions[decision.id] = asdict(decision)
        _write(self.decisions_path, decisions)

    @staticmethod
    def _normalize(text: str) -> str:
        return " ".join(text.lower().split())

    @staticmethod
    def _conclusion(item: dict) -> Conclusion:
        item = dict(item)
        item["evidence"] = [Evidence(**evidence) for evidence in item["evidence"]]
        return Conclusion(**item)

    @staticmethod
    def _read(path: Path) -> dict:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


class MessageBus:
    """Thread-safe typed events shared by Lead and specialist Agents."""

    def __init__(self):
        self._inboxes: dict[str, list[dict[str, Any]]] = {}
        self._lock = threading.Lock()

    def send(self, sender: str, target: str, content: str,
             message_type: str = "message", task_id: str | None = None) -> None:
        event = {"id": _id("msg"), "time": _now(), "from": sender, "to": target,
                 "type": message_type, "task_id": task_id, "content": content}
        with self._lock:
            self._inboxes.setdefault(target, []).append(event)

    def read_inbox(self, name: str) -> list[dict[str, Any]]:
        with self._lock:
            return self._inboxes.pop(name, [])


class AgentTeam:
    """Bounded background specialists with cancellation, timeout and reassignment."""

    def __init__(self, runtime: Any, client_factory: Callable, model: str,
                 runner: Callable | None = None, max_concurrency: int = 4,
                 timeout_seconds: float = 60.0):
        self.runtime, self.client_factory, self.model = runtime, client_factory, model
        self.runner = runner
        self.max_concurrency, self.timeout_seconds = max_concurrency, timeout_seconds
        self.BUS = MessageBus()
        self.active_teammates: dict[str, threading.Thread] = {}
        self.records: dict[str, AgentRecord] = {}
        self.cancel_flags: dict[str, threading.Event] = {}
        self.store = WorkflowStore(runtime.data_dir)
        request_human = (lambda payload: runtime.approvals.request_approval(
            "resolve_agent_conflict", payload).id) if hasattr(runtime, "approvals") else None
        human_approved = (lambda approval_id: bool(
            runtime.approvals.get_approval(approval_id)
            and runtime.approvals.get_approval(approval_id)["status"] == "approved")
            if hasattr(runtime, "approvals") else False)
        self.arbitrator = ConflictArbitrator(runtime.data_dir, request_human, human_approved)
        self._lock = threading.RLock()

    def _agent_runner(self) -> Callable:
        if self.runner:
            return self.runner
        from research_agent import agent_loop
        return agent_loop

    def spawn_subagent(self, name: str, role: str, prompt: str,
                       task_id: str | None = None) -> str:
        from research_agent import normalize_mcp_name
        safe_name = normalize_mcp_name(name)
        role_key = ROLE_ALIASES.get(role.lower(), role.lower())
        spec = ROLE_SPECS.get(role_key)
        if not spec:
            return f"Error: unknown role '{role}'"
        with self._lock:
            active = sum(thread.is_alive() for thread in self.active_teammates.values())
            if active >= self.max_concurrency:
                return f"Error: maximum {self.max_concurrency} concurrent subagents reached"
            if safe_name in self.active_teammates and self.active_teammates[safe_name].is_alive():
                return f"Subagent '{safe_name}' already exists"
            record = AgentRecord(safe_name, role_key, prompt, task_id or _id("task"))
            cancel = threading.Event()
            self.records[safe_name], self.cancel_flags[safe_name] = record, cancel
            self.store.save_agent(record)

        def run() -> None:
            trace_id = self.runtime.traces.start_trace("subtask", "multi")
            started = time.perf_counter()
            record.status, record.started_at = "running", _now()
            self.store.save_agent(record)
            self.store.event("agent_started", {"agent": safe_name, "task_id": record.task_id})
            try:
                if cancel.is_set():
                    raise RuntimeError("cancelled")
                client = self.client_factory()
                protocol = {"required_input": spec.input_fields,
                            "required_output": spec.output_fields}
                messages = [{"role": "user", "content":
                             f"Role: {spec.name}. {spec.purpose}\nProtocol: {protocol}\nTask: {prompt}"}]
                summary = self._agent_runner()(client, messages, self.runtime, self.model,
                                               max_rounds=12,
                                               allowed_tool_names=set(spec.allowed_tools),
                                               session_id=f"agent-{safe_name}",
                                               agent_name=safe_name,
                                               trace_id=trace_id, mode="multi",
                                               task_type="subtask")
                if cancel.is_set():
                    raise RuntimeError("cancelled")
                record.status, record.result = "completed", summary
                self.BUS.send(safe_name, "lead", summary, "result", record.task_id)
            except Exception as exc:
                if record.status != "timed_out":
                    record.status = "cancelled" if cancel.is_set() else "failed"
                record.error = str(exc)
                self.BUS.send(safe_name, "lead", f"Subagent error: {exc}",
                              record.status, record.task_id)
            finally:
                duration = (time.perf_counter() - started) * 1000
                self.runtime.traces.record_span(
                    trace_id, "subtask", safe_name,
                    "success" if record.status == "completed" else "failed",
                    duration, error=record.error,
                    metadata={"status": record.status, "attempts": record.attempts,
                              "task_id": record.task_id})
                self.runtime.traces.finish_trace(
                    trace_id, "completed" if record.status == "completed" else record.status,
                    record.error, 1.0 if record.status == "completed" else 0.0)
                record.finished_at = _now()
                self.store.save_agent(record)
                self.store.event("agent_finished", {"agent": safe_name,
                                                     "status": record.status})

        thread = threading.Thread(target=run, daemon=True, name=f"agent-{safe_name}")
        with self._lock:
            self.active_teammates[safe_name] = thread
        thread.start()
        return f"Subagent '{safe_name}' spawned as {role_key}"

    def cancel_subagent(self, name: str) -> str:
        flag = self.cancel_flags.get(name)
        if not flag:
            return f"Subagent '{name}' not found"
        flag.set()
        record = self.records[name]
        record.status, record.finished_at = "cancelled", _now()
        self.store.save_agent(record)
        return f"Subagent '{name}' cancellation requested"

    def check_timeouts(self) -> list[str]:
        timed_out = []
        now = datetime.now(timezone.utc)
        for name, record in list(self.records.items()):
            thread = self.active_teammates.get(name)
            if record.status != "running" or not thread or not thread.is_alive():
                continue
            started = datetime.fromisoformat(record.started_at) if record.started_at else now
            if (now - started).total_seconds() > self.timeout_seconds:
                self.cancel_flags[name].set()
                record.status, record.error, record.finished_at = "timed_out", "timeout", _now()
                self.store.save_agent(record)
                timed_out.append(name)
        return timed_out

    def reassign_failed(self, name: str, replacement: str | None = None) -> str:
        record = self.records.get(name) or self.store.load_agent(name)
        if not record or record.status not in {"failed", "timed_out", "cancelled"}:
            return f"Error: subagent '{name}' has no failed task to reassign"
        next_name = replacement or f"{name}-retry-{record.attempts + 1}"
        result = self.spawn_subagent(next_name, record.role, record.prompt, record.task_id)
        if result.startswith("Subagent"):
            self.records[next_name].attempts = record.attempts + 1
            self.store.save_agent(self.records[next_name])
        return result

    def collect_subagent_results(self) -> str:
        self.check_timeouts()
        messages = self.BUS.read_inbox("lead")
        return json.dumps(messages, ensure_ascii=False) if messages else "No subagent results yet"

    def run_research_workflow(self, research_question: str) -> WorkflowState:
        """Run Planner -> Researcher -> Writer -> Reviewer with durable checkpoints."""
        tasks, previous = [], []
        for role in ROLE_SPECS:
            task_id = _id("step")
            tasks.append(WorkflowTask(task_id, role, depends_on=list(previous)))
            previous = [task_id]
        state = WorkflowState(_id("workflow"), research_question, "running", tasks)
        self.store.save_workflow(state)
        self.runtime.memory.save_working(state.id, [task.role for task in tasks])
        return self._continue_workflow(state)

    def resume_workflow(self, workflow_id: str) -> WorkflowState:
        state = self.store.load_workflow(workflow_id)
        if not state:
            raise KeyError(workflow_id)
        if state.status == "completed":
            return state
        state.status = "running"
        return self._continue_workflow(state)

    def rollback_workflow(self, workflow_id: str, checkpoint_id: str) -> WorkflowState:
        state = self.store.restore(workflow_id, checkpoint_id)
        state.status = "paused"
        self.store.save_workflow(state)
        return state

    def _continue_workflow(self, state: WorkflowState) -> WorkflowState:
        trace_id = self.runtime.traces.start_trace("research_task", "multi", state.id)
        context: dict[str, Any] = {"research_question": state.research_question}
        for role, text in state.results.items():
            try:
                context.update(json.loads(text))
            except json.JSONDecodeError:
                context[role] = text
        for task in state.tasks:
            if task.status == "completed":
                continue
            checkpoint_id = self.store.snapshot(state, f"before-{task.role}")
            task.status = "running"
            self.store.save_workflow(state)
            spec = ROLE_SPECS[task.role]
            missing_inputs = [field for field in spec.input_fields if field not in context]
            if missing_inputs:
                task.status, state.status = "failed", "failed"
                task.result = f"missing role inputs: {', '.join(missing_inputs)}"
                self.runtime.memory.update_progress(state.id, task.role, "failed")
                self.store.save_workflow(state)
                self.runtime.traces.finish_trace(trace_id, "failed", task.result, 0.0)
                return state
            prompt = json.dumps(context, ensure_ascii=False)
            last_error = None
            for attempt in range(2):
                started = time.perf_counter()
                try:
                    client = self.client_factory()
                    messages = [{"role": "user", "content":
                                 f"Role: {task.role}. {spec.purpose}\nInput: {prompt}\n"
                                 f"Return exactly one JSON object with fields: {spec.output_fields}"}]
                    text = self._agent_runner()(client, messages, self.runtime, self.model,
                                                max_rounds=12,
                                                allowed_tool_names=set(spec.allowed_tools),
                                                session_id=f"workflow-{state.id}-{task.role}-{attempt}",
                                                agent_name=task.role,
                                                trace_id=trace_id, mode="multi",
                                                task_type="research_task")
                    result = parse_role_result(task.role, task.role, text)
                    task.status, task.result = "completed", result.content
                    state.results[task.role] = result.content
                    context.update(result.payload)
                    self.runtime.memory.update_progress(state.id, task.role, "completed")
                    for evidence in result.evidence:
                        self.runtime.memory.add_evidence(state.id, evidence.content,
                                                         evidence.source, evidence.confidence)
                    self.store.event("workflow_step_completed", {"workflow": state.id,
                                                                   "role": task.role,
                                                                   "attempt": attempt + 1})
                    self.runtime.traces.record_span(
                        trace_id, "subtask", task.role, "success",
                        (time.perf_counter() - started) * 1000,
                        metadata={"attempt": attempt + 1, "status": task.status})
                    last_error = None
                    break
                except Exception as exc:
                    last_error = exc
                    self.runtime.traces.record_span(
                        trace_id, "subtask", task.role, "failed",
                        (time.perf_counter() - started) * 1000, error=str(exc),
                        metadata={"attempt": attempt + 1, "status": "retrying"})
                    self.store.event("workflow_step_failed", {"workflow": state.id,
                                                                "role": task.role,
                                                                "attempt": attempt + 1,
                                                                "error": str(exc)})
            if last_error is not None:
                restored = self.store.restore(state.id, checkpoint_id)
                restored.status = "paused"
                failed_task = next(item for item in restored.tasks if item.id == task.id)
                failed_task.result = str(last_error)
                self.runtime.memory.update_progress(state.id, task.role, "failed")
                self.store.save_workflow(restored)
                self.runtime.traces.finish_trace(trace_id, "paused", str(last_error), 0.0)
                return restored
            state.updated_at = _now()
            self.store.save_workflow(state)
        state.status, state.updated_at = "completed", _now()
        self.store.save_workflow(state)
        self.runtime.traces.finish_trace(trace_id, "completed", quality=1.0)
        return state
