# CLI Coding Agent MVP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a usable single-agent CLI that safely reads, patches, and tests the repository from which it is launched.

**Architecture:** Create one self-contained teaching implementation in `hyj/coding-agent/coding_agent.py`, extracting only the relevant ideas from `s20_comprehensive/code.py`. Keep workspace safety, tools, permissions, context management, recovery, logging, verification, and CLI orchestration as separately testable functions/classes inside that file. Do not modify any lesson file.

**Tech Stack:** Python 3.10+, Anthropic Messages API, `python-dotenv`, Python standard library, pytest.

## Progress checkpoint (2026-08-12)

- [x] Created `hyj/coding-agent/coding_agent.py` and `hyj/coding-agent/test_coding_agent.py`.
- [x] Implemented and tested workspace path validation, file reading/search, exact patching, new-file creation, diff preview, command results, basic permission classification, tool dispatch, JSONL logging, retry, context compaction, Agent Loop, CLI help, and per-turn state reset.
- [x] Current focused verification: `19 passed`.
- [x] Full repository verification with third-party pytest plugin autoload disabled: `42 passed`.
- [x] Completed a read-only sub-Agent code review.
- [ ] Address the security and protocol findings below before calling the CLI production-usable.

### Next-session priority findings

1. Treat all arbitrary command execution as approval-required; the current string deny list is not a security boundary and cannot prevent Python/Node/scripts from accessing files outside the workspace.
2. Replace command-string parsing with an argv-array tool schema to avoid Windows quoting errors and shell ambiguity.
3. Always return one `tool_result` for every `tool_use`, including calls rejected after the tool-call limit.
4. Make patch approval atomic by verifying the approved original-content hash immediately before writing.
5. Strengthen log redaction and avoid logging full source/patch/tool output by default.
6. Make compaction preserve complete tool-use/result groups and handle `max_tokens` as an incomplete response.
7. Derive changed files from canonical paths plus Git/runtime snapshots; improve verification detection without forcing Jest-only arguments.
8. Add missing tests for output truncation, approvals, malformed/multiple tools, limits, retry exhaustion, CLI interruption/configuration, verification, Git summaries, and secret handling.

**Resume point:** Start with finding 1 using TDD. Do not add advanced features until findings 1–8 are resolved. The implementation now lives under `hyj/coding-agent/`.

## Global Constraints

- The first version is a local single-Agent CLI, normally launched in the VS Code terminal.
- The launch directory is the workspace; file tools must reject paths outside it.
- Include file search/read/patch, shell execution, diff approval, test detection, retry/recovery, limits, compaction, logs, Git summary, Ctrl+C handling, and a final report.
- Exclude multi-Agent, Cron, persistent task boards, long-term memory, worktrees, and Skills from the MVP. MCP is excluded from every Coding Agent route and belongs to the separate assistant/workflow Agent project.
- Preserve the educational readability of this repository; do not introduce a framework.

---

### Task 1: Workspace-safe file and search tools

**Files:**
- Create: `hyj/coding-agent/coding_agent.py`
- Create: `hyj/coding-agent/test_coding_agent.py`

**Interfaces:**
- Produces: `resolve_workspace_path(path: str, workspace: Path) -> Path`
- Produces: `run_read(path: str, workspace: Path, offset: int = 0, limit: int = 400) -> str`（工具名为 `read_file`）
- Produces: `run_glob(pattern: str, workspace: Path) -> str`（工具名为 `glob`）
- Produces: `apply_patch(path: str, old_text: str, new_text: str, workspace: Path) -> str`

- [x] Write tests proving traversal is rejected, reads support offset/limit, glob search returns relative paths, and patching requires exactly one old-text match.
- [x] Run `python -m pytest hyj/coding-agent/test_coding_agent.py -v` and confirm the tests fail because the module does not exist.
- [x] Extract and tighten the path/read/glob/edit behavior from s20 lines 379–445. Use `Path.resolve()` plus `is_relative_to()`, UTF-8 file access, sorted relative search results, and exact replacement.
- [x] Run the focused test file and confirm all Task 1 tests pass.

### Task 2: Command execution, permissions, diffs, and project verification

**Files:**
- Modify: `hyj/coding-agent/coding_agent.py`
- Modify: `hyj/coding-agent/test_coding_agent.py`

**Interfaces:**
- Produces: `CommandResult` dataclass with `command`, `returncode`, `stdout`, `stderr`, `timed_out`
- Produces: `classify_command(command: str) -> str` returning `allow`, `ask`, or `deny`
- Produces: `run_bash(command: str, workspace: Path, timeout: int = 60) -> CommandResult`（工具名为 `bash`）
- Produces: `detect_verification_commands(workspace: Path) -> list[str]`
- Produces: `git_summary(workspace: Path) -> str`
- Produces: `preview_patch(path: str, old_text: str, new_text: str, workspace: Path) -> str`

- [ ] Add tests for deny-listed commands, approval-required commands, timeout handling, output truncation, unified diff generation, and detection of pytest/npm verification commands.
- [ ] Run the focused tests and confirm the new cases fail.
- [ ] Implement a permission policy that denies obvious system-destruction commands, asks for deletion/permission-changing commands, and allows ordinary read/test/build commands. Execute through `subprocess.run(..., cwd=workspace, capture_output=True, text=True, timeout=timeout)` without `shell=True`; tokenize with `shlex.split(..., posix=False)` on Windows.
- [ ] Detect `python -m pytest -q` from pytest indicators and `npm test -- --runInBand`/`npm run build` from `package.json` scripts. Generate diffs with `difflib.unified_diff`; obtain Git status and diff read-only.
- [ ] Run the focused tests and confirm Task 2 passes.

### Task 3: Tool schemas, dispatch, approval, and logging

**Files:**
- Modify: `hyj/coding-agent/coding_agent.py`
- Modify: `hyj/coding-agent/test_coding_agent.py`

**Interfaces:**
- Produces: `BUILTIN_TOOLS: list[dict]` for `glob`, `read_file`, `apply_patch`, and `bash`
- Produces: `ToolRuntime.execute(name: str, args: dict) -> str`
- Produces: `SessionLogger.log(event: str, data: dict) -> None`
- Consumes: an injectable `approve(prompt: str) -> bool` callback

- [ ] Add tests confirming schemas have required fields, unknown tools return a stable error, patches are previewed before approval, denied patches do not change files, sensitive commands request approval, and JSONL logs contain tool start/result/error events.
- [ ] Run focused tests and confirm failure.
- [ ] Build the dispatch layer around Task 1–2 functions. Store logs at `.coding-agent/session-<timestamp>.jsonl`, excluding API keys and `.env` contents. Return tool failures as strings so the LLM can recover instead of crashing the CLI.
- [ ] Run focused tests and confirm Task 3 passes.

### Task 4: Agent loop, limits, retry, and context compaction

**Files:**
- Modify: `hyj/coding-agent/coding_agent.py`
- Modify: `hyj/coding-agent/test_coding_agent.py`

**Interfaces:**
- Produces: `AgentLimits(max_rounds=30, max_tool_calls=80, max_context_chars=120000)` dataclass
- Produces: `RecoveryState` dataclass
- Produces: `compact_messages(messages: list, max_chars: int) -> list`
- Produces: `with_retry(fn, state, max_retries: int = 4)`
- Produces: `agent_loop(client, messages: list, runtime: ToolRuntime, model: str, limits: AgentLimits) -> AgentReport`

- [ ] Create fake-client tests for a normal final answer, one tool call round-trip, multiple tool calls, rate-limit retry without real sleeping, tool/round limit termination, malformed tool arguments, and preservation of recent messages during compaction.
- [ ] Run focused tests and confirm failure.
- [ ] Adapt the s20 agent-loop pattern: append assistant content, execute every `tool_use`, append matching `tool_result` blocks, and continue until final text or a limit. Retry 429/529 errors with injected sleep, compact only at message boundaries, and never split a tool-use/tool-result pair.
- [ ] Run focused tests and confirm Task 4 passes.

### Task 5: CLI session, automatic verification, and final report

**Files:**
- Modify: `hyj/coding-agent/coding_agent.py`
- Modify: `hyj/coding-agent/test_coding_agent.py`

**Interfaces:**
- Produces: `AgentReport` with `final_text`, `changed_files`, `verification`, `remaining_issues`, and `stopped_reason`
- Produces: `build_system_prompt(workspace: Path) -> str`
- Produces: `run_cli(workspace: Path | None = None) -> int`

- [ ] Add tests for workspace selection, `.env` model configuration, empty input exit, Ctrl+C return code, verification result inclusion, and final Git-based changed-file summary.
- [ ] Run focused tests and confirm failure.
- [ ] Implement the CLI with `Path.cwd()` as default workspace, `MODEL_ID` configuration, persistent in-process conversation history, colored but optional status messages, safe interrupt handling, and final reporting. Instruct the model to inspect before editing, verify relevant changes, and state unverified work honestly.
- [ ] Run `python -m pytest hyj/coding-agent/test_coding_agent.py -v` and then `python -m pytest -q`.
- [ ] Run `python hyj/coding-agent/coding_agent.py --help` and a no-network smoke test using the fake client path.

## Self-review

- Coverage: every confirmed MVP feature is assigned to Tasks 1–5; excluded features are not introduced.
- Scope: one implementation file plus one focused test file; lesson sources remain unchanged.
- Safety: destructive commands, workspace escape, timeouts, output limits, approval, and interruption have explicit tests.
- Consistency: tool names and signatures are fixed in Tasks 1–3 and consumed unchanged by later tasks.
