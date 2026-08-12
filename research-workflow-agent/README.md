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
- 审批通过后投递到本地 outbox 的通知流程
- JSON-RPC stdio MCP 客户端、动态工具发现与分发
- 后台科研子 Agent、角色隔离和 Lead 消息收件箱

当前已完成 Stage 1–4，共有 15 项项目内自动化测试。MCP 客户端已经实现，但 Zotero、GitHub 等具体服务器需要用户另行选择、安装和配置。

真实外部通知连接器和端到端研究周报将在后续阶段实现，详见 [`implementation-plan.md`](implementation-plan.md)。总体范围和演示流程见 [`../agent-projects-plan.md`](../agent-projects-plan.md)。

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

测试：

```powershell
python -m pytest test_research_agent.py -q
```
