"""Deterministic, local demo for the three-tier research memory system."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from memory_system import MemoryStore


TASK_ID = "task_memory_demo"
SESSION_ID = "session_memory_demo"


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _reset_demo_files(data_dir: Path) -> None:
    """Reset only files owned by this demo; never remove a directory tree."""
    data_dir.mkdir(parents=True, exist_ok=True)
    for name in ("working_memory.json", "short_term_memory.json",
                 "long_term_memory.json", "memory.json"):
        path = data_dir / name
        if path.exists():
            path.unlink()


def build_demo(output_dir: Path) -> dict[str, Any]:
    data_dir = output_dir.resolve() / ".research-agent"
    _reset_demo_files(data_dir)

    # The lower message limit makes compression visible with only three Q&A rounds.
    memory = MemoryStore(data_dir, max_long_term=10, max_session_messages=4)

    memory.save_working(
        TASK_ID,
        plan=["确定研究问题", "检索论文", "整理证据", "撰写结论"],
        progress={"确定研究问题": "completed", "检索论文": "in_progress"},
    )
    memory.add_evidence(
        TASK_ID,
        "论文 A 将 Agent 记忆划分为工作、短期和长期记忆。",
        "paper-A.pdf#page=3",
        0.92,
    )
    memory.update_progress(TASK_ID, "检索论文", "completed")
    memory.update_progress(TASK_ID, "整理证据", "in_progress")

    turns = [
        ("user", "我准备研究 Agent 记忆系统。"),
        ("assistant", "可以先比较工作记忆、短期记忆和长期记忆。"),
        ("user", "短期记忆主要保存什么？"),
        ("assistant", "短期记忆保存当前会话的近期问答和历史摘要。"),
        ("user", "长期记忆如何进入上下文？"),
        ("assistant", "每轮根据当前问题检索相关长期记忆并加入 Prompt。"),
    ]
    for role, content in turns:
        memory.append_turn(SESSION_ID, role, content)

    memory.remember(
        "用户偏好使用中文解释，并希望给出具体代码位置。",
        category="preference",
        source="user",
        confidence=1.0,
    )
    memory.remember(
        "用户当前研究方向是 Agent 记忆系统和多 Agent 协作。",
        category="research_direction",
        source="user",
        confidence=0.95,
    )
    memory.remember(
        "三级记忆由工作记忆、短期记忆和长期记忆组成。",
        category="confirmed_conclusion",
        source="reviewer",
        confidence=0.9,
        reviewed_by="reviewer",
    )

    rejected = ""
    try:
        memory.remember(
            "未经确认的子 Agent 猜测不应写入长期记忆。",
            source="researcher",
            confidence=0.3,
            confirmed=False,
        )
    except ValueError as exc:
        rejected = str(exc)

    query = "请继续解释 Agent 三级记忆"
    context = json.loads(memory.build_context(query, SESSION_ID, limit=5))
    result = {
        "data_dir": str(data_dir),
        "working_memory": _read_json(data_dir / "working_memory.json"),
        "short_term_memory": _read_json(data_dir / "short_term_memory.json"),
        "long_term_memory": _read_json(data_dir / "long_term_memory.json"),
        "context_for_next_loop": context,
        "rejected_untrusted_write": rejected,
    }

    working = result["working_memory"][TASK_ID]
    session = result["short_term_memory"][SESSION_ID]
    assert len(working["evidence"]) == 1
    assert working["progress"]["整理证据"] == "in_progress"
    assert len(session["messages"]) == 4
    assert "我准备研究 Agent 记忆系统" in session["summary"]
    assert len(result["long_term_memory"]) == 3
    assert len(context["recalled_long_term_memory"]) >= 2
    assert rejected
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the deterministic three-tier memory demo")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).with_name("memory-demo-output"),
        help="Directory that will contain the isolated .research-agent demo data",
    )
    args = parser.parse_args()
    result = build_demo(args.output)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print("\nAll fixed memory demo checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
