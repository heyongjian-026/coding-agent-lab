"""Privacy-aware Bad Case collection, review, retrieval and replay."""

from __future__ import annotations

import json
import re
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


CASE_CATEGORIES = {"retrieval", "memory", "planning", "tool", "collaboration", "output"}
CASE_TYPES = {
    "correction": "output", "downvote": "output", "rejection": "output",
    "retrieval": "retrieval", "citation": "output", "weekly_report": "output",
    "memory": "memory", "agent_conflict": "collaboration", "planning": "planning",
    "tool": "tool", "collaboration": "collaboration", "output": "output",
}
SENSITIVE_KEYS = {"api_key", "apikey", "token", "password", "secret", "authorization"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def redact(value: Any) -> Any:
    """Recursively remove credentials and common personal identifiers."""
    if isinstance(value, dict):
        return {key: ("[REDACTED]" if key.lower() in SENSITIVE_KEYS else redact(item))
                for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    text = str(value)
    text = re.sub(r"(?i)bearer\s+[a-z0-9._-]+", "Bearer [REDACTED]", text)
    text = re.sub(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b", "[REDACTED_EMAIL]", text)
    text = re.sub(r"(?<!\d)1[3-9]\d{9}(?!\d)", "[REDACTED_PHONE]", text)
    text = re.sub(r"\b(?:sk|pk)-[A-Za-z0-9_-]{12,}\b", "[REDACTED_KEY]", text)
    return text


@dataclass
class BadCase:
    id: str
    case_type: str
    category: str
    task_input: Any
    trajectory: list[dict]
    result: Any
    failure_stage: str
    source: str
    review_status: str = "pending"
    approval_id: str | None = None
    root_cause: str = ""
    fixed_version: str = ""
    retest_result: str = ""
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)


class BadCaseStore:
    """Candidate cases are isolated until an approved human review accepts them."""

    def __init__(self, data_dir: Path, request_human: Callable[[dict], str] | None = None,
                 human_approved: Callable[[str], bool] | None = None):
        self.candidates_path = data_dir / "bad_case_candidates.json"
        self.library_path = data_dir / "bad_case_library.json"
        self.replays_path = data_dir / "bad_case_replays.jsonl"
        self.request_human, self.human_approved = request_human, human_approved
        self._lock = threading.RLock()

    def collect(self, case_type: str, task_input: Any, trajectory: list[dict] | None,
                result: Any, failure_stage: str, source: str = "system",
                category: str | None = None) -> BadCase:
        if case_type not in CASE_TYPES:
            raise ValueError(f"Unsupported Bad Case type '{case_type}'")
        category = category or CASE_TYPES[case_type]
        if category not in CASE_CATEGORIES:
            raise ValueError(f"Unsupported Bad Case category '{category}'")
        item = BadCase(_id("case"), case_type, category, redact(task_input),
                       redact(trajectory or []), redact(result), failure_stage, source)
        if self.request_human:
            item.approval_id = self.request_human({"case_id": item.id,
                                                   "case_type": case_type,
                                                   "category": category})
        with self._lock:
            cases = self._read(self.candidates_path)
            cases[item.id] = asdict(item)
            self._write(self.candidates_path, cases)
        return item

    def collect_feedback(self, feedback_type: str, task_input: Any, result: Any,
                         comment: str, trajectory: list[dict] | None = None) -> BadCase:
        if feedback_type not in {"correction", "downvote", "rejection"}:
            raise ValueError("feedback_type must be correction, downvote, or rejection")
        return self.collect(feedback_type, task_input, trajectory,
                            {"result": result, "user_comment": comment},
                            "user_feedback", source="user")

    def review(self, case_id: str, approve: bool, reviewer: str) -> BadCase:
        with self._lock:
            candidates = self._read(self.candidates_path)
            if case_id not in candidates:
                raise KeyError(case_id)
            item = BadCase(**candidates[case_id])
            if not item.approval_id or not self.human_approved or not self.human_approved(item.approval_id):
                raise PermissionError("Human approval is required before Bad Case review")
            item.review_status = "accepted" if approve else "rejected"
            item.updated_at = _now()
            candidates[case_id] = asdict(item)
            self._write(self.candidates_path, candidates)
            if approve:
                library = self._read(self.library_path)
                accepted = asdict(item)
                accepted["reviewer"] = reviewer
                library[case_id] = accepted
                self._write(self.library_path, library)
            return item

    def record_remediation(self, case_id: str, root_cause: str,
                           fixed_version: str, retest_result: str) -> BadCase:
        with self._lock:
            candidates = self._read(self.candidates_path)
            if case_id not in candidates:
                raise KeyError(case_id)
            item = BadCase(**candidates[case_id])
            item.root_cause, item.fixed_version = redact(root_cause), fixed_version
            item.retest_result, item.updated_at = redact(retest_result), _now()
            candidates[case_id] = asdict(item)
            self._write(self.candidates_path, candidates)
            library = self._read(self.library_path)
            if case_id in library:
                library[case_id].update({"root_cause": item.root_cause,
                                         "fixed_version": fixed_version,
                                         "retest_result": item.retest_result,
                                         "updated_at": item.updated_at})
                self._write(self.library_path, library)
            return item

    def search(self, query: str = "", category: str | None = None,
               case_type: str | None = None, accepted_only: bool = True) -> list[dict]:
        items = self._read(self.library_path if accepted_only else self.candidates_path)
        words = set(query.lower().split())
        results = []
        for item in items.values():
            if category and item["category"] != category:
                continue
            if case_type and item["case_type"] != case_type:
                continue
            text = json.dumps(item, ensure_ascii=False).lower()
            if words and not all(word in text for word in words):
                continue
            results.append(item)
        return sorted(results, key=lambda item: item["updated_at"], reverse=True)

    def replay(self, case_id: str, replay_fn: Callable[[dict], Any] | None = None) -> dict:
        item = self._read(self.library_path).get(case_id)
        if not item:
            raise ValueError("Only accepted Bad Cases can be replayed")
        package = {"case_id": case_id, "task_input": item["task_input"],
                   "trajectory": item["trajectory"], "previous_result": item["result"]}
        replay_result = replay_fn(package) if replay_fn else package
        record = {"time": _now(), "case_id": case_id, "result": redact(replay_result)}
        with self._lock:
            with self.replays_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        return record

    @staticmethod
    def _read(path: Path) -> dict:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    @staticmethod
    def _write(path: Path, value: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)
