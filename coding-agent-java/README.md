# Coding Agent Java

这是 `../coding-agent` Python 项目的 Java 17 迁移版。它不是逐行翻译，而是按职责拆分为安全文件工具、命令沙箱、工具运行时、混合规划、Agent Loop、模型客户端和离线评测器。

## 已实现能力

- `glob`、`read_file`、`apply_patch`、`bash` 四种结构化工具；文件只能访问工作区，拒绝 `.env`。
- Patch 先展示差异、人工批准，再用原文 SHA-256 防止批准后被替换。
- 命令使用 `argv`，不经过 shell；策略拒绝破坏性命令和工作区外路径，其他命令均需人工批准。
- Docker 沙箱默认断网、只读根文件系统、移除 capabilities、限制 CPU/内存/进程数，仅挂载工作区。
- 工具级和相同命令级熔断、错误分类、恢复建议、瞬时 API 指数退避。
- 按复杂度选择 ReAct 或 Plan-and-Execute，维护步骤状态并进行有限重规划和返工。
- 独立完成门禁检查变更范围、验证证据和计划状态。
- JSONL 审计日志、敏感信息脱敏、提示词注入告警。
- 12 条确定性离线回放、7 项量化指标、版本化基线比较。

## 构建与测试

```powershell
cd hyj/coding-agent-java
mvn test
```

当前共有 27 个 JUnit 测试；其中一个测试会运行完整的 12 条离线评测集。也可以单独运行评测器：

```powershell
mvn -q -DskipTests package
mvn -q exec:java -Dexec.mainClass=com.hyj.codingagent.EvalRunner `
  -Dexec.args="--dataset evals/tasks.json --output evals/latest-results.json --baseline evals/baseline.json"
```

## 运行 CLI

在本目录创建不提交的 `.env`：

```text
ANTHROPIC_API_KEY=...
ANTHROPIC_BASE_URL=https://api.anthropic.com
MODEL_ID=claude-sonnet-4-5
```

然后运行：

```powershell
mvn -q exec:java -Dexec.mainClass=com.hyj.codingagent.CodingAgentCli `
  -Dexec.args="--workspace D:\path\to\project --sandbox docker"
```

`--sandbox local` 只用于开发和测试，不构成安全边界。Docker 后端需要本机 Docker daemon 已启动，且所用镜像已经存在或可以拉取。

## 类职责

- `WorkspaceTools`：工作区文件访问、Patch、注入告警、验证命令发现。
- `CommandSupport`：命令协议、策略、审批票据、本地/Docker 后端、错误分类。
- `ToolRuntime`：审批执行、状态审计、熔断和验证证据。
- `Planning`：复杂度路由、任务计划和完成条件检查。
- `AgentLoop`：模型—工具循环、重试、上下文压缩、重规划和返工。
- `AnthropicHttpClient`：基于 Java `HttpClient` 的 Messages API 适配器。
- `EvalRunner`：离线轨迹回放、指标统计与基线回归。
