# Coding Agent Lab

一个用于学习 Agent Harness 的本地 CLI Coding Agent。它可以在启动目录中搜索、读取和修改代码，并调用测试或构建命令进行验证。

## 当前状态（2026-08-13）

MVP 功能已经实现。现有能力包括安全文件工具、混合执行架构、完成条件检查、错误恢复、离线评测和自动回归。

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

## 离线评测与回归

`evals/tasks.json` 包含 12 个不调用真实模型的确定性轨迹案例，覆盖 Bug 修复、功能增加、测试修复和危险操作拒绝。每个案例保存任务输入、初始文件、期望工具序列、标准文件结果和验收条件。

运行评测并与指标基线比较：

```powershell
python eval_runner.py --dataset evals/tasks.json --output evals/latest-results.json --baseline evals/baseline.json
```

报告统计任务完成率、测试通过率、无关修改率、工具选择准确率、参数准确率、平均修复轮数和危险操作拦截率。指标低于基线（反向指标高于基线）时命令返回非零状态。GitHub Actions 在相关代码提交或 Pull Request 时自动运行聚焦测试和评测；本项目不启用定时任务和真实模型测试。

## 文件

- `coding_agent.py`：Coding Agent MVP。
- `test_coding_agent.py`：自动化测试。
- `eval_runner.py`：离线评测、指标统计和基线比较入口。
- `evals/tasks.json`：12 个版本化评测案例。
- `evals/baseline.json`：回归指标基线。
- `../agent-projects-plan.md`：两个 Agent 项目的总体规划。
- `coding-agent-implementation-plan.md`：实现计划、当前进度和下一步安全改进。

## 来源与许可

本项目基于 [shareAI-lab/learn-claude-code](https://github.com/shareAI-lab/learn-claude-code) 的教学代码与思路进行提取和改造，采用 MIT License。
