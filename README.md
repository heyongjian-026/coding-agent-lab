# Agent Projects Lab

本仓库用于实现和对比两个 Agent 项目。

## 目录

- `coding-agent/`：本地 CLI Coding Agent，负责读取、修改、测试代码并安全执行工具。
- `research-workflow-agent/`：面向计算机专业研究生的科研工作流 Agent。
- `agent-projects-plan.md`：两个项目的总体规划和功能边界。
- `note`：学习笔记。

## 当前进度

| 项目 | 状态 | 已实现 |
|---|---|---|
| Coding Agent | MVP 完善阶段 | 文件工具、Patch/Diff、命令执行、审批、Agent Loop、重试、上下文压缩、日志与测试 |
| Research Workflow Agent | Stage 4 完成 | 向量 RAG、长期记忆、任务看板、Cron、审批通知、MCP 动态工具与科研子 Agent |

两个项目当前共有 34 项自动化测试。下一步分别是 Coding Agent 安全加固，以及科研工作流 Agent 的端到端研究周报流程。

## 文档入口

- [Coding Agent 使用说明](coding-agent/README.md)
- [Research Workflow Agent 使用说明](research-workflow-agent/README.md)
- [项目总体规划](agent-projects-plan.md)

## 来源与许可

本仓库基于 [shareAI-lab/learn-claude-code](https://github.com/shareAI-lab/learn-claude-code) 的教学代码与思路进行提取和扩展，采用 MIT License。
