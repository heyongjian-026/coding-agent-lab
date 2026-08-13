# Research Workflow Agent Implementation Plan

## Progress

Current checkpoint (verified 2026-08-13): Stage 4 complete, DashScope-compatible embeddings and Elasticsearch storage configured, and 17 focused tests passing. MCP server commands remain user-supplied configuration; no Zotero or GitHub server is bundled or automatically installed.

- [x] Stage 1: single-Agent loop and Anthropic-compatible CLI
- [x] Stage 1: persistent research task board
- [x] Stage 1: durable memory
- [x] Stage 1: local lexical RAG
- [x] Stage 1: human approval records
- [x] Stage 1: persistent Cron jobs and due-job injection
- [x] Stage 2: pluggable local embedding model and persistent vector index
- [x] Stage 2 extension: DashScope-compatible batch embeddings and Elasticsearch vector store
- [x] Stage 2: approval-gated notification delivery to a local outbox
- [x] Stage 3: JSON-RPC stdio MCP client, configured server boundary, and dynamic tools
- [x] Stage 4: specialized background research sub-Agents and message bus
- [ ] Stage 5: weekly-report workflow and end-to-end evaluation

## Naming

Concepts shared with `s20_comprehensive/code.py` retain familiar names where practical: `Task`, `create_task`, `save_task`, `load_task`, `claim_task`, `complete_task`, `CronJob`, `cron_matches`, `validate_cron`, `TOOLS`, `TOOL_HANDLERS`, `RecoveryState`, `with_retry`, and `agent_loop`.

## Next checkpoint

Implement Stage 5 with tests: compose retrieval, task state, specialist results and approval-gated notification into a weekly-report workflow and evaluate it end to end.
