# Coding Agent Lab

一个用于学习 Agent Harness 的本地 CLI Coding Agent。它可以在启动目录中搜索、读取和修改代码，并调用测试或构建命令进行验证。

## 当前状态

项目处于 MVP 开发阶段，已经实现文件工具、Patch/Diff、命令执行、权限确认、Agent Loop、重试、上下文压缩、日志和基础测试。当前版本尚未经过安全加固，不应在不可信代码仓库或生产环境中运行。

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
- `agent-projects-plan.md`：两个 Agent 项目的功能规划。
- `coding-agent-implementation-plan.md`：实现计划、当前进度和下一步安全改进。

## 来源与许可

本项目基于 [shareAI-lab/learn-claude-code](https://github.com/shareAI-lab/learn-claude-code) 的教学代码与思路进行提取和改造，采用 MIT License。

