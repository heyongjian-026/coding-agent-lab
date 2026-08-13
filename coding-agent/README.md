# Coding Agent Lab

一个用于学习 Agent Harness 的本地 CLI Coding Agent。它可以在启动目录中搜索、读取和修改代码，并调用测试或构建命令进行验证。

## 当前状态（2026-08-13）

MVP 功能已经实现，项目进入安全加固和测试补全阶段。现有能力包括文件工具、Patch/Diff、命令执行、权限确认、Agent Loop、重试、上下文压缩、日志和基础测试。聚焦测试已重新验证为 `19 passed`。

当前版本仍不应在不可信代码仓库或生产环境中运行。下一步优先处理任意命令审批、argv 工具协议、完整的 `tool_use`/`tool_result` 配对、Patch 原子校验、日志脱敏和上下文压缩边界。

## 运行

```powershell
pip install anthropic python-dotenv pytest
$env:MODEL_ID="your-model-id"
python coding_agent.py --workspace D:\path\to\project
```

运行测试：

```powershell
python -m pytest test_coding_agent.py -v
```

## 文件

- `coding_agent.py`：Coding Agent MVP。
- `test_coding_agent.py`：自动化测试。
- `../agent-projects-plan.md`：两个 Agent 项目的总体规划。
- `coding-agent-implementation-plan.md`：实现计划、当前进度和下一步安全改进。

## 来源与许可

本项目基于 [shareAI-lab/learn-claude-code](https://github.com/shareAI-lab/learn-claude-code) 的教学代码与思路进行提取和改造，采用 MIT License。
