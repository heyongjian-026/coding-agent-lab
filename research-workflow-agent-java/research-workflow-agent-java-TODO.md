# Research Workflow Agent Java To-do List

定位：Java 17 实现的可持久化、可降级、可恢复、可观测科研工作流。为避免重复，本清单将 Python 版 78 条合并为 23 个功能点。`【√】` 表示 Java 版已实现，`【△】` 表示本地/基础实现，`【×】` 表示原项目尚未完成且 Java 版也保留待办。

## P0：三级记忆【DOCX-6】

1. 【√】定义并分别持久化工作、短期和长期记忆；工作记忆保存计划、来源证据和执行进度。（代码：`MemoryStore.java:19-73`；Agent 接入：`MultiAgent.java:144-158`）
2. 【√】短期记忆保存多轮会话、超限摘要和会话恢复；Agent Loop 在模型调用前压缩旧上下文。（代码：`MemoryStore.java:77-112`；接入：`ResearchAgent.java:13-38`）
3. 【√】长期记忆实施稳定性、确认状态和可信来源门禁，并保存时间、置信度和来源。（代码：`MemoryStore.java:116-141`）
4. 【√】长期记忆支持中英文相关性召回、纠错替换、遗忘、冲突检测、同类合并和置信度淘汰。（代码：`MemoryStore.java:131-183, 202-245`）
5. 【√】构造包含短期摘要与长期召回结果的模型上下文，防止未审核检索结果直接污染长期记忆。（代码：`MemoryStore.java:186-190`；Prompt：`ResearchAgent.java:46-47`）

## P0：科研多 Agent【DOCX-2】

6. 【√】实现 Planner、Researcher、Writer、Reviewer 四角色及明确输入、输出和受限工具权限。（代码：`MultiAgent.java:20-50`）
7. 【√】持久化 Agent 身份、任务、状态和结果；提供线程安全 Lead 收件箱，并限制并发、递归创建、取消、超时和失败重分配。（代码：`MultiAgent.java:52-63, 111-143`）
8. 【√】实现“规划—检索—写作—审核”端到端流程，角色输出执行 JSON 协议校验，证据同步到工作记忆。（代码：`MultiAgent.java:36-50, 144-158`）

## P1：冲突仲裁与回滚【DOCX-7】

9. 【√】结论必须附带证据和可信度；识别同一问题的冲突，置信度差距不足时请求人工审批。（代码：`MultiAgent.java:80-105`）
10. 【√】只有获批结论可以进入长期记忆；工作流步骤前保存快照，失败局部重试、回滚、暂停和检查点续跑。（代码：`MultiAgent.java:65-78, 101-108, 144-158`）

## P1：备用模型与服务【DOCX-5】

11. 【√】统一模型接口，按任务复杂度选择轻量/强模型，并按主备顺序自动切换。（代码：`Resilience.java:24-35, 75-97`）
12. 【√】远程 Embedding 失败时切换本地哈希模型；Elasticsearch 失败时切换本地向量知识库。（代码：`KnowledgeService.java:27-50, 88-114`；降级：`Resilience.java:100-128`）
13. 【√】降级过程使用 operation_id、结果缓存、追加事件和幂等待处理任务，避免重复外部写入。（代码：`Resilience.java:17-72`）
14. 【√】MCP 支持备用 Server 切换，全部失败时保存待处理任务；支持基于 argv 的持久化 stdio JSON-RPC 传输。（代码：`McpSupport.java:18-62`；接入：`ResearchRuntime.java:40-53, 121-126`）
15. 【△】支持 OpenAI-compatible Embedding 和 Elasticsearch kNN HTTP API；真实服务部署与认证集成测试仍需具体环境。（代码：`KnowledgeService.java:52-85, 117-151`）

## P1：Bad Case 回流【DOCX-9】

16. 【√】收集纠正、点踩、拒绝以及检索、记忆、工具、协作错误，统一保存脱敏输入、轨迹、结果和失败阶段。（代码：`BadCaseStore.java:16-53`；自动回流：`ResearchRuntime.java:62-65`）
17. 【√】候选案例需人工审核才能进入正式库；支持类别检索、根因/修复版本/复测结果记录和确定性重放。（代码：`BadCaseStore.java:56-107`）

## P1：链路监控【DOCX-11】

18. 【√】用 trace_id 串联主 Agent、子 Agent、模型和工具，记录 Token、组件耗时、重试状态、失败原因、费用和总时长。（代码：`TraceStore.java:13-58`；接入：`ResearchAgent.java:13-38, MultiAgent.java:133-158, ResearchRuntime.java:62-65`）
19. 【√】支持 Token、时间和费用预算；超限时暂停并只创建一次人工审批，批准后继续。（代码：`TraceStore.java:61-77`；接入：`ResearchAgent.java:20-22`）
20. 【√】统计任务/周报成功率和组件失败率，比较单 Agent 与多 Agent 的质量、费用和延迟，生成单次 Trace 报告。（代码：`TraceStore.java:79-101`；工具：`ResearchRuntime.java:103-107`）

## 科研业务基础组件

21. 【√】持久化科研任务依赖与生命周期、人工审批、通知 Outbox 和同一分钟去重的五字段 Cron。（代码：`ResearchStores.java:18-163`）
22. 【√】动态组装 38 个内置/MCP 工具 Schema；MCP 有副作用工具必须审批，已批准动作执行后标记 executed。（代码：`ResearchRuntime.java:56-65, 67-119, 131-159`）
23. 【√】Agent Loop 支持记忆召回、工具调用/结果配对、最大轮数、临时 API 重试、Token 记录和最终会话持久化。（代码：`ResearchAgent.java:10-50`；模型 HTTP：`AnthropicResearchClient.java:14-24`；CLI：`ResearchAgentCli.java:11-21`）

## 测试与持续回归

24. 【√】28 个 JUnit 测试覆盖三级记忆、任务/审批/Cron、向量检索、服务降级、仲裁回滚、MCP、Bad Case、Trace、工具权限和 Agent Loop。（测试：`MemoryAndStoresTest.java:1-87, MultiAgentTest.java:1-81, ResilienceAndObservabilityTest.java:1-64, RuntimeLoopAndBadCaseTest.java:1-74`）
25. 【√】Maven Java 17 构建以及 Push/Pull Request 自动回归。（构建：`pom.xml:1-45`；CI：`.github/workflows/research-workflow-agent-java-regression.yml:1-27`）

## 原项目尚未完成的外部集成

26. 【×】选择并接入真实 Zotero、GitHub 或学术 MCP Server，增加真实服务集成测试。（待实现，无代码范围）
27. 【×】接入真实邮件或企业消息通知渠道；当前只实现经审批的本地 Outbox。（待实现，无代码范围）
28. 【×】实现独立常驻 Cron 服务、进程重启恢复和跨进程去重。（待实现，无代码范围）
29. 【×】准备完整论文、笔记、实验数据和周报演示脚本，形成可展示的真实端到端周报。（待实现，无代码范围）

## RAG 质量增强

30. 【√】【新增】实现标题/段落/句末优先切分、固定长度兜底、Chunk 重叠及章节和偏移元数据。（代码：`KnowledgeService.java:52-60, 75`）
31. 【√】【新增】实现向量与 BM25 混合召回、RRF 融合和查询词覆盖率重排。（代码：`KnowledgeService.java:56-61, 80-82`）
32. 【√】【新增】为本地哈希检索和远程语义检索设置不同的最低证据门槛，拒绝无关结果。（代码：`KnowledgeService.java:61, 70, 80`）
33. 【√】【新增】保存可追溯 `citation_id`，校验最终回答的引用来源，失败时要求模型返修。（代码：`KnowledgeService.java:52-53, 60, 69, 78`；Agent：`ResearchAgent.java:15-43, 50-56`）
34. 【√】【新增】建立 20 条标准问题和 1 条无答案负样本的 RAG 评测集，统计 Recall@3、MRR、引用元数据完整率和拒答率。（数据：`evals/rag-tasks.json:1-38`；代码：`RagEvalRunner.java:12-29`；测试：`RagPipelineTest.java:10-56`）
