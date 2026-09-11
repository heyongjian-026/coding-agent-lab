# Coding Agent Java To-do List

定位：Java 17 实现的安全、可验证、可恢复 Coding Agent。为减少重复，本清单把 Python 版 67 条合并为 20 个可独立讲解和验收的功能点。`【√】` 表示 Java 版已实现，`【△】` 表示只实现本地基础版本。

## P0：安全执行【DOCX-12】

1. 【√】工作区文件边界、符号链接/相对路径逃逸检查和 `.env` 敏感文件拒绝。（代码：`WorkspaceTools.java:38-60, 137-144`；接入：`ToolRuntime.java:97-107`）
2. 【√】Patch 单次精确替换、Diff 审批、批准时原文哈希复核和临时文件原子写入。（代码：`WorkspaceTools.java:79-116`；接入：`ToolRuntime.java:110-124`）
3. 【√】命令采用 `argv` 协议；独立策略拒绝破坏性命令和工作区外路径；任意允许候选仍需人工审批。（代码：`CommandSupport.java:27-50, 85-132`；接入：`ToolRuntime.java:129-161`）
4. 【√】审批票据绑定命令及超时指纹，执行前复核，避免“批准 A、执行 B”。（代码：`CommandSupport.java:68-79`；接入：`ToolRuntime.java:150-156`）
5. 【√】可替换的本地/Docker 沙箱；Docker 断网、只读根目录、移除 capabilities、禁止提权、限制资源且只挂载工作区。（代码：`CommandSupport.java:135-209`）
6. 【△】实现本地 JSONL 审计、Patch 内容省略、输出截断、API Key/Bearer Token 脱敏；集中式审计平台不在本项目范围。（代码：`SessionLogger.java:20-84`；接入：`ToolRuntime.java:74-94, 146-156`）
7. 【△】读取不可信文件时检测常见提示词注入并告警；授权仍由确定性策略控制，不承诺覆盖未知攻击。（代码：`WorkspaceTools.java:27-32, 120-123`；接入：`ToolRuntime.java:97-107`）

## P0：熔断与错误恢复【DOCX-4】

8. 【√】分类权限、超时、参数、命令和永久错误，并生成确定性恢复动作建议。（代码：`CommandSupport.java:212-231`；接入：`ToolRuntime.java:164-182`）
9. 【√】统计同工具同类连续失败并熔断；限制同一命令反复失败后的自动修复次数。（代码：`ToolRuntime.java:74-94, 129-143, 164-182`）
10. 【√】限制 Agent 轮数、工具总调用数和返工数；瞬时 429/529/过载错误有限重试。（代码：`AgentLoop.java:19-22, 60-141, 144-157`）
11. 【√】最终报告汇总停止原因、遗留问题、已改文件、验证证据、恢复建议和计划状态。（代码：`AgentLoop.java:42-53, 126-139`；展示：`CodingAgentCli.java:72-82`）

## P1：ReAct 与 Plan-and-Execute 混合架构【DOCX-1】

12. 【√】按任务长度、复杂关键词、分段和编号选择直接 ReAct 或先规划后执行。（代码：`Planning.java:155-180`；循环入口：`AgentLoop.java:60-70`）
13. 【√】维护 inspect/implement/verify/review 步骤及 pending/in-progress/completed/failed 状态。（代码：`Planning.java:27-102`）
14. 【√】根据工具结果推进计划；局部失败反馈给模型，连续失败或验收失败时有限重规划。（代码：`Planning.java:87-149`；循环：`AgentLoop.java:91-119`）

## P1：反思与自我质检【DOCX-3】

15. 【√】模型回答之外设置完成门禁，检查变更覆盖、修改范围、验证证据和计划完成度。（代码：`Planning.java:184-204`；循环：`AgentLoop.java:82-99`）
16. 【√】自动发现 Maven、Gradle、pytest 和 npm 验证命令，并只把成功工具结果视为证据。（代码：`WorkspaceTools.java:126-135`；执行：`ToolRuntime.java:157-159, 185-194`）
17. 【√】完成检查失败时按实现、验证或复核问题触发返工；达到上限后如实停止。（代码：`Planning.java:131-149`；循环：`AgentLoop.java:82-99`）

## P1：离线评测与回归【DOCX-8、DOCX-10】

18. 【√】保留 12 条确定性轨迹，覆盖 Bug 修复、功能、测试修复和危险操作；每条含输入、初始文件、工具轨迹和验收条件。（数据：`evals/tasks.json:1-91`）
19. 【√】回放轨迹并统计完成率、测试通过率、无关修改率、工具/参数准确率、修复轮数和危险操作拦截率；生成 JSON 并比较基线。（代码：`EvalRunner.java:39-162`；基线：`evals/baseline.json:1-22`）
20. 【√】27 个 JUnit 测试覆盖核心边界并执行完整离线评测；Push/PR 自动测试、比较基线并上传结果。（测试：`WorkspaceAndSecurityTest.java:1-95, RuntimeAndPlanningTest.java:1-99, AgentLoopAndEvalTest.java:1-78`；CI：`.github/workflows/coding-agent-java-regression.yml:1-36`）

## Java 运行入口

- 【√】基于 Java `HttpClient` 的 Anthropic Messages API、四种工具 JSON Schema 和响应解析。（代码：`AnthropicHttpClient.java:23-99`）
- 【√】CLI 支持 `.env`、模型/工作区/沙箱参数、多轮会话和人工审批。（代码：`CodingAgentCli.java:13-82`）
