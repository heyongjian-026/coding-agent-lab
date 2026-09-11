# Research Workflow Agent Java

这是 `../research-workflow-agent` 的 Java 17 迁移版，主攻三级记忆、科研多 Agent、冲突仲裁、外部服务降级、Bad Case 回流和全链路监控。

## 已实现能力

- 工作记忆保存任务计划、证据与进度；短期记忆持久化会话并压缩旧消息；长期记忆执行可信来源门禁、检索、纠错、合并和淘汰。
- Planner → Researcher → Writer → Reviewer 四角色科研工作流，具有明确输入/输出协议和工具白名单。
- 后台子 Agent 支持并发限制、取消、超时检测、失败重分配、Lead 收件箱和持久化身份状态。
- 每个工作流步骤前保存检查点；步骤失败会局部重试，仍失败则回滚并暂停，随后可以恢复。
- 冲突结论必须附带来源和可信度；差距明显时自动仲裁，接近时请求人工审批；只有通过的结论可以写入长期记忆。
- 模型按任务复杂度路由，并支持主备模型；Embedding、Elasticsearch 和 MCP 支持本地或备用服务降级。
- MCP 支持 stdio JSON-RPC、工具发现、只读提示和有副作用工具审批；全部数据源失败时保存幂等待处理任务。
- 用户反馈及检索、记忆、工具、协作错误可脱敏进入候选 Bad Case，经人工审核后进入正式库并支持归因和重放。
- trace_id 串联模型、工具和子任务，记录 Token、耗时、费用、失败率、成功率，并支持预算暂停和人工 override。
- 任务依赖、审批、通知 Outbox 和五字段 Cron 调度。

## 构建与测试

```powershell
cd hyj/research-workflow-agent-java
mvn test
```

测试不调用真实模型、Elasticsearch 或 MCP 网络服务，外部服务通过可替换接口和 Fake Transport 验证。

## 运行 CLI

复制 `.env.example` 为 `.env`，填入模型配置：

```powershell
mvn -q exec:java -Dexec.mainClass=com.hyj.researchagent.ResearchAgentCli `
  -Dexec.args="--workspace D:\path\to\research-workspace"
```

运行数据保存在工作区的 `.research-agent/`。通知只写入本地 `notification_outbox.jsonl`，不会真的发送邮件。

## 主要类

- `MemoryStore`：三级记忆。
- `MultiAgent`：角色协议、子 Agent、冲突仲裁、快照与工作流。
- `ResearchRuntime`：工具池和组件总装。
- `KnowledgeService`：Embedding、向量知识库和 Elasticsearch。
- `Resilience`：模型和服务降级、幂等日志。
- `BadCaseStore`：失败案例回流。
- `TraceStore`：Token、费用、延迟、预算和指标。
- `McpSupport`：stdio MCP 与工具协议。
- `ResearchAgent`：带记忆召回和监控的 Agent Loop。

## 当前边界

与原 Python 项目一致，真实 Zotero/学术 MCP、真实通知渠道、独立常驻 Cron 服务以及完整周报演示数据仍属于后续集成工作；核心接口和本地可测试实现已经具备。

## RAG 检索链路

- 文档优先按标题、段落和句末切分，长段落固定长度兜底；默认保留重叠，并记录章节、起止偏移、内容哈希和 `citation_id`。
- 本地知识库并行计算向量相似度和 BM25，以 RRF 融合后按查询词覆盖率轻量重排；低相关结果不会强行返回。
- Elasticsearch 查询同时提交全文 `multi_match` 和 kNN 向量检索，并对候选结果执行相同的应用层重排。
- Agent 使用知识库证据后必须以 `[[citation_id]]` 引用；缺失或伪造引用会进入一次回答修复循环。
- `evals/rag-tasks.json` 包含 20 条可回答问题和 1 条无答案负样本。运行 `RagEvalRunner` 可统计 Recall@3、MRR、引用元数据完整率和无关查询拒答率。

```powershell
mvn -q exec:java "-Dexec.mainClass=com.hyj.researchagent.RagEvalRunner" "-Dexec.args=evals/rag-tasks.json"
```
