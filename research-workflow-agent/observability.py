"""Trace, metrics, cost and budget controls for research Agent workflows."""

from __future__ import annotations

import contextvars
import json
import os
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


@dataclass
class TraceBudget:
    max_tokens: int | None = None
    max_seconds: float | None = None
    max_cost: float | None = None

    @classmethod
    def from_env(cls) -> "TraceBudget":
        return cls(_optional_int("TRACE_MAX_TOKENS"), _optional_float("TRACE_MAX_SECONDS"),
                   _optional_float("TRACE_MAX_COST"))


@dataclass
class TraceRecord:
    id: str
    task_type: str
    mode: str
    status: str
    started_at: str
    started_monotonic: float
    input_tokens: int = 0
    output_tokens: int = 0
    cost: float = 0.0
    duration_ms: float = 0.0
    quality: float | None = None
    budget: dict[str, Any] = field(default_factory=dict)
    budget_approval_id: str | None = None
    budget_override: bool = False
    error: str = ""
    finished_at: str | None = None


@dataclass
class SpanRecord:
    trace_id: str
    span_id: str
    component: str
    operation: str
    status: str
    duration_ms: float
    input_tokens: int = 0
    output_tokens: int = 0
    cost: float = 0.0
    error: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    time: str = field(default_factory=_now)


class TraceStore:
    """File-backed trace collector shared by the Lead and child Agents."""

    def __init__(self, data_dir: Path, budget: TraceBudget | None = None,
                 request_human: Callable[[dict], str] | None = None,
                 human_approved: Callable[[str], bool] | None = None):
        self.traces_path = data_dir / "traces.json"
        self.spans_path = data_dir / "trace_spans.jsonl"
        self.default_budget = budget or TraceBudget.from_env()
        self.request_human, self.human_approved = request_human, human_approved
        self.input_price = float(os.getenv("MODEL_INPUT_PRICE_PER_MILLION", "0"))
        self.output_price = float(os.getenv("MODEL_OUTPUT_PRICE_PER_MILLION", "0"))
        self._current: contextvars.ContextVar[str | None] = contextvars.ContextVar(
            "research_trace_id", default=None)
        self._lock = threading.RLock()

    def start_trace(self, task_type: str = "research_task", mode: str = "single",
                    trace_id: str | None = None, budget: TraceBudget | None = None) -> str:
        trace_id = trace_id or f"trace_{uuid.uuid4().hex}"
        with self._lock:
            traces = self._read_traces()
            if trace_id in traces:
                traces[trace_id]["duration_offset_ms"] = traces[trace_id].get("duration_ms", 0.0)
                traces[trace_id]["started_monotonic"] = time.monotonic()
                traces[trace_id]["status"] = "running"
                traces[trace_id]["error"] = ""
            else:
                selected = budget or self.default_budget
                traces[trace_id] = asdict(TraceRecord(
                    trace_id, task_type, mode, "running", _now(), time.monotonic(),
                    budget=asdict(selected)))
            self._write(self.traces_path, traces)
        self._current.set(trace_id)
        return trace_id

    def activate(self, trace_id: str) -> None:
        if trace_id not in self._read_traces():
            raise KeyError(trace_id)
        self._current.set(trace_id)

    @property
    def current_trace_id(self) -> str | None:
        return self._current.get()

    def measure(self, component: str, operation: str, fn: Callable,
                metadata: dict[str, Any] | None = None):
        trace_id = self.current_trace_id
        if not trace_id:
            return fn()
        started = time.perf_counter()
        try:
            result = fn()
            self.record_span(trace_id, component, operation, "success",
                             (time.perf_counter() - started) * 1000, metadata=metadata)
            return result
        except Exception as exc:
            self.record_span(trace_id, component, operation, "failed",
                             (time.perf_counter() - started) * 1000,
                             error=str(exc), metadata=metadata)
            raise

    def record_model(self, trace_id: str, operation: str, duration_ms: float,
                     response: Any = None, error: str = "") -> None:
        usage = getattr(response, "usage", None)
        input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
        output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
        cost = ((input_tokens * self.input_price + output_tokens * self.output_price)
                / 1_000_000)
        self.record_span(trace_id, "model", operation,
                         "failed" if error else "success", duration_ms,
                         input_tokens, output_tokens, cost, error)
        with self._lock:
            traces = self._read_traces()
            trace = traces[trace_id]
            trace["input_tokens"] += input_tokens
            trace["output_tokens"] += output_tokens
            trace["cost"] = round(trace["cost"] + cost, 8)
            self._write(self.traces_path, traces)

    def record_span(self, trace_id: str, component: str, operation: str, status: str,
                    duration_ms: float, input_tokens: int = 0, output_tokens: int = 0,
                    cost: float = 0.0, error: str = "",
                    metadata: dict[str, Any] | None = None) -> None:
        span = SpanRecord(trace_id, f"span_{uuid.uuid4().hex}", component, operation,
                          status, round(duration_ms, 3), input_tokens, output_tokens,
                          round(cost, 8), error, metadata or {})
        with self._lock:
            self.spans_path.parent.mkdir(parents=True, exist_ok=True)
            with self.spans_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(asdict(span), ensure_ascii=False) + "\n")

    def check_budget(self, trace_id: str) -> dict[str, Any] | None:
        with self._lock:
            traces = self._read_traces()
            trace = traces[trace_id]
            if trace.get("budget_override"):
                return None
            approval_id = trace.get("budget_approval_id")
            if approval_id and self.human_approved and self.human_approved(approval_id):
                trace["budget_override"] = True
                traces[trace_id] = trace
                self._write(self.traces_path, traces)
                return None
            budget = trace["budget"]
            elapsed = ((trace.get("duration_offset_ms", 0.0) / 1000)
                       + time.monotonic() - trace["started_monotonic"])
            total_tokens = trace["input_tokens"] + trace["output_tokens"]
            exceeded = []
            if budget.get("max_tokens") is not None and total_tokens >= budget["max_tokens"]:
                exceeded.append("tokens")
            if budget.get("max_seconds") is not None and elapsed >= budget["max_seconds"]:
                exceeded.append("time")
            if budget.get("max_cost") is not None and trace["cost"] >= budget["max_cost"]:
                exceeded.append("cost")
            if not exceeded:
                return None
            if not approval_id and self.request_human:
                approval_id = self.request_human({"trace_id": trace_id, "exceeded": exceeded})
                trace["budget_approval_id"] = approval_id
                traces[trace_id] = trace
                self._write(self.traces_path, traces)
            return {"trace_id": trace_id, "exceeded": exceeded,
                    "approval_id": approval_id}

    def finish_trace(self, trace_id: str, status: str, error: str = "",
                     quality: float | None = None) -> None:
        with self._lock:
            traces = self._read_traces()
            trace = traces[trace_id]
            trace["status"], trace["error"], trace["quality"] = status, error, quality
            trace["duration_ms"] = round(
                trace.get("duration_offset_ms", 0.0)
                + (time.monotonic() - trace["started_monotonic"]) * 1000, 3)
            trace["finished_at"] = _now()
            self._write(self.traces_path, traces)

    def report(self, trace_id: str) -> dict[str, Any]:
        trace = self._read_traces().get(trace_id)
        if not trace:
            raise KeyError(trace_id)
        spans = [span for span in self._read_spans() if span["trace_id"] == trace_id]
        return {"trace": trace, "spans": spans}

    def metrics(self) -> dict[str, Any]:
        traces = list(self._read_traces().values())
        finished = [item for item in traces if item["status"] != "running"]
        successful = [item for item in finished if item["status"] == "completed"]
        weekly = [item for item in finished if item["task_type"] == "weekly_report"]
        spans = self._read_spans()
        components = {}
        for span in spans:
            item = components.setdefault(span["component"], {"calls": 0, "failures": 0})
            item["calls"] += 1
            item["failures"] += span["status"] == "failed"
        return {
            "task_success_rate": len(successful) / len(finished) if finished else 0.0,
            "weekly_report_success_rate": (sum(item["status"] == "completed" for item in weekly)
                                           / len(weekly) if weekly else 0.0),
            "component_failure_rate": {
                name: item["failures"] / item["calls"] for name, item in components.items()},
            "total_cost": round(sum(item["cost"] for item in traces), 8),
            "average_duration_ms": (sum(item["duration_ms"] for item in finished)
                                    / len(finished) if finished else 0.0),
        }

    def compare_modes(self) -> dict[str, dict[str, float]]:
        groups: dict[str, list[dict]] = {}
        for trace in self._read_traces().values():
            if trace["status"] != "running":
                groups.setdefault(trace["mode"], []).append(trace)
        result = {}
        for mode, traces in groups.items():
            qualities = [item["quality"] for item in traces if item["quality"] is not None]
            result[mode] = {
                "quality": sum(qualities) / len(qualities) if qualities else 0.0,
                "cost": sum(item["cost"] for item in traces) / len(traces),
                "latency_ms": sum(item["duration_ms"] for item in traces) / len(traces),
            }
        return result

    def _read_traces(self) -> dict:
        return json.loads(self.traces_path.read_text(encoding="utf-8")) if self.traces_path.exists() else {}

    def _read_spans(self) -> list[dict]:
        if not self.spans_path.exists():
            return []
        return [json.loads(line) for line in self.spans_path.read_text(encoding="utf-8").splitlines()
                if line.strip()]

    @staticmethod
    def _write(path: Path, value: Any) -> None:
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)


class InstrumentedEmbedding:
    """Record remote or local embedding latency in the active trace."""

    def __init__(self, model: Any, traces: TraceStore, name: str):
        self.model, self.traces, self.name = model, traces, name
        self.dimensions = model.dimensions

    def embed_many(self, texts: list[str]) -> list[list[float]]:
        return self.traces.measure("embedding", self.name,
                                   lambda: self.model.embed_many(texts),
                                   {"batch_size": len(texts)})

    def embed(self, text: str) -> list[float]:
        return self.embed_many([text])[0]


def _optional_int(name: str) -> int | None:
    value = os.getenv(name, "").strip()
    return int(value) if value else None


def _optional_float(name: str) -> float | None:
    value = os.getenv(name, "").strip()
    return float(value) if value else None
