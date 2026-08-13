# Research Workflow Agent

面向计算机专业研究生的科研工作流 Agent，用于辅助论文调研、阅读计划、实验管理和研究进展总结。

## 已实现

- 本地关键词 RAG：资料分块、持久化与检索
- 长期研究记忆
- 带依赖关系的持久化科研任务看板
- 人工审批请求与审批状态
- 持久化 Cron 任务和到期任务注入
- Anthropic 兼容 API 的 Agent Loop 与工具调用
- 可插拔本地 Embedding 与持久化向量检索
- DashScope 兼容 Embedding API 与 Elasticsearch 向量存储
- 审批通过后投递到本地 outbox 的通知流程
- JSON-RPC stdio MCP 客户端、动态工具发现与分发
- 后台科研子 Agent、角色隔离和 Lead 消息收件箱

当前已完成 Stage 1–4，共有 17 项项目内自动化测试。MCP 客户端已经实现，但 Zotero、GitHub 等具体服务器需要用户另行选择、安装和配置。

Stage 5 将实现端到端研究周报与评估。真实外部通知连接器仍属于后续工作，详见 [`implementation-plan.md`](implementation-plan.md)。总体范围和演示流程见 [`../agent-projects-plan.md`](../agent-projects-plan.md)。

## 运行

复制 `.env.example` 为 `.env` 并填写模型配置，然后执行：

```powershell
python research_agent.py --workspace D:\path\to\research-workspace
```

通知必须由用户在 CLI 中审批，Agent 不能自行批准：

```text
/approve approval_...
/reject approval_...
```

## MCP 配置

将 `mcp_servers.example.json` 的结构复制到研究工作区的 `.research-agent/mcp_servers.json`，填写已安装 MCP 服务器的 argv 数组。Agent 只能按名称连接配置中已有的服务器，不能自行提供启动命令。

连接后，服务器工具会以 `mcp__服务器名__工具名` 加入正常工具池。示例配置中的命令只是占位符，不会自动安装 Zotero 或 GitHub MCP 服务器。

MCP 服务器明确标注 `annotations.readOnlyHint: true` 的工具可以直接读取；其他工具一律创建审批请求，用户执行 `/approve approval_...` 后才会真正调用。

## RAG 配置

在 `.env` 中配置 DashScope 兼容 Embedding API 和 Elasticsearch。填写 `EMBEDDING_API_KEY` 后，CLI 会使用 `text-embedding-v4` 生成 2048 维向量，并将知识分块写入 Elasticsearch；单次 Embedding 请求最多发送 10 条。未配置 `EMBEDDING_API_URL` 或 `EMBEDDING_MODEL` 时仍使用本地哈希向量与 JSON 存储，便于离线教学和测试。

Elasticsearch 默认连接 `http://localhost:9200`，索引名为 `research-agent-knowledge`。启动前需要确保服务可访问，并填写实际的用户名和密码。

测试：

```powershell
python -m pytest test_research_agent.py -q
```
