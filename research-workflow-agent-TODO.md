# Research Workflow Agent To-do List

定位：具备长期记忆、外部服务降级、多 Agent 协作和链路监控的科研工作流。`【√】`表示已完成，`【×】`表示待完成；`【原有】`表示本轮前已有，`【新增】`表示本轮新增或后续待增加。

## 最终分工

| DOCX 条目 | 主项目 | Research Workflow Agent 处理方式 |
| --- | --- | --- |
| 1. ReAct 与 Plan-and-Execute | Coding Agent | 仅保留基础 Agent Loop |
| 2. Planner、Executor、Reviewer | Research Workflow Agent | 完整实践科研多 Agent 分工 |
| 3. Agent 反思与自我质检 | Coding Agent | 仅由 Reviewer 做必要结果检查 |
| 4. 连续失败熔断与降级 | Coding Agent | 仅保留外部调用所需基础重试 |
| 5. 备用模型或备用 API | Research Workflow Agent | 模型、Embedding、存储和数据源降级 |
| 6. 工作、短期、长期三级记忆 | Research Workflow Agent | 完整实践科研记忆体系 |
| 7. 多 Agent 冲突仲裁与状态回滚 | Research Workflow Agent | 结论仲裁与任务恢复 |
| 8. 离线评测集和量化指标 | Coding Agent | 只保留科研功能必要测试 |
| 9. 用户 Bad Case 自动回流 | Research Workflow Agent | 建立科研失败案例回流管线 |
| 10. 定期回归测试 | Coding Agent | 只维护普通项目测试 |
| 11. Token、延迟、任务成功率监控 | Research Workflow Agent | 监控完整工作流和子 Agent |
| 12. 权限隔离、安全沙箱、审计与合规 | Coding Agent | 只保留 MCP 和通知审批边界 |

主攻范围：`DOCX-2、5、6、7、9、11`。

## P0：三级记忆【DOCX-6】

1. 【√】【原有】持久化科研任务、状态、依赖和负责人。（代码：`research_agent.py:53-110`）
2. 【√】【原有】CLI 当前进程保留多轮会话消息。（代码：`research_agent.py:1057-1113`）
3. 【√】【原有】长期保存并检索研究偏好、事实和结论。（代码：`memory_system.py:193-213`）
4. 【√】【新增】明确划分工作、短期和长期记忆的数据结构。（代码：`memory_system.py:44-71, 74-92`）
5. 【√】【新增】工作记忆保存计划、中间证据和执行进度。（代码：`memory_system.py:107-141`；接入：`research_agent.py:744-746, 858-866`）
6. 【√】【新增】短期记忆持久化最近对话并支持会话恢复。（代码：`memory_system.py:144-164`；接入：`research_agent.py:978-983, 1057-1060`）
7. 【√】【新增】长期记忆只保存稳定偏好和已确认结论。（代码：`memory_system.py:193-204`）
8. 【√】【新增】制定写入、召回、更新、淘汰和遗忘规则。（代码：`memory_system.py:193-232, 262-273`）
9. 【√】【新增】增加记忆冲突检测、合并和纠错。（代码：`memory_system.py:215-254`）
10. 【√】【新增】为记忆保存来源、时间和可信度。（代码：`memory_system.py:44-71, 125-131, 193-204`）
11. 【√】【新增】防止检索及子 Agent 错误污染长期记忆。（代码：`memory_system.py:78-81, 193-204`）
12. 【√】【新增】为 Agent Loop 增加上下文压缩和记忆召回。（代码：`memory_system.py:167-190, 256-260`；接入：`research_agent.py:970-1000`）

## P0：科研多 Agent【DOCX-2】

13. 【√】【原有】支持后台科研子 Agent、Lead 收件箱和消息总线。（代码：`multi_agent.py:358-374, 377-517`）
14. 【√】【原有】限制子 Agent 递归创建其他 Agent。（代码：`multi_agent.py:40-62, 406-476`；接入：`research_agent.py:644-654`）
15. 【√】【原有】子 Agent 使用受限工具池。（代码：`multi_agent.py:40-62, 435-445`；接入：`research_agent.py:644-654`）
16. 【√】【新增】实现 Planner、Researcher、Writer 和 Reviewer 四个角色。（代码：`multi_agent.py:31-67, 519-626`）
17. 【√】【新增】为角色定义输入、输出和工具权限。（代码：`multi_agent.py:31-62, 435-445, 560-585`）
18. 【√】【新增】建立结构化任务、证据和结果协议。（代码：`multi_agent.py:69-160`）
19. 【√】【新增】使用统一状态对象和事件通信。（代码：`multi_agent.py:107-243, 358-374`）
20. 【√】【新增】限制并发子 Agent 数量。（代码：`multi_agent.py:377-423`）
21. 【√】【新增】支持子 Agent 超时、取消和失败重分配。（代码：`multi_agent.py:406-512`；工具接入：`research_agent.py:760-767, 887-894`）
22. 【√】【新增】持久化子 Agent 身份、任务和执行状态。（代码：`multi_agent.py:107-243, 406-470`）
23. 【√】【新增】实现“规划—检索—写作—审核”端到端流程。（代码：`multi_agent.py:519-626`；工具接入：`research_agent.py:768-770, 895-896`）

## P1：冲突仲裁与回滚【DOCX-7】

24. 【√】【新增】识别不同 Agent 对同一问题的冲突结论。（代码：`multi_agent.py:141-150, 256-277`）
25. 【√】【新增】要求结论附带证据和可信度。（代码：`multi_agent.py:69-73, 141-149, 256-272`）
26. 【√】【新增】由 Lead/Supervisor 仲裁，必要时请求人工确认。（代码：`multi_agent.py:243-340`；接入：`research_agent.py:682-692, 777-780, 903-906, 1067-1078`）
27. 【√】【新增】未审核结果不得进入正式长期记忆。（代码：`multi_agent.py:57-60, 304-336`；门控：`memory_system.py:193-204`）
28. 【√】【新增】任务执行前保存可恢复状态快照。（代码：`multi_agent.py:163-242, 546-559`）
29. 【√】【新增】子任务失败时局部回滚并支持从检查点继续。（代码：`multi_agent.py:213-227, 531-544, 612-620`）
30. 【√】【新增】防止单个子 Agent 失败拖垮工作流。（代码：`multi_agent.py:406-476, 571-620`）

## P1：备用模型与服务【DOCX-5】

31. 【√】【原有】未配置远程 Embedding 时使用本地哈希模型。（代码：`research_agent.py:118-135, 294-325`）
32. 【√】【原有】支持远程 Embedding、Elasticsearch 和本地知识库。（代码：`research_agent.py:137-325`）
33. 【√】【新增】配置统一模型接口、主模型和备用模型。（代码：`resilience.py:27-136`；接入：`research_agent.py:326-345, 1049-1055`）
34. 【√】【新增】主模型/API 不可用时自动切换。（代码：`resilience.py:35-136`）
35. 【√】【新增】按照任务复杂度选择轻量或强模型。（代码：`resilience.py:19-24, 106-136`；接入：`research_agent.py:984, 1002-1005`）
36. 【√】【新增】运行中 Embedding 失败时降级到本地模型。（代码：`resilience.py:138-154, 156-183`；接入：`research_agent.py:294-325`）
37. 【√】【新增】Elasticsearch 失败时降级到本地知识库。（代码：`resilience.py:156-183`；接入：`research_agent.py:294-325`）
38. 【√】【新增】MCP 失败时切换数据源或保存待处理任务。（代码：`research_agent.py:656-680, 806-840`；通用降级：`resilience.py:35-104`）
39. 【√】【新增】保证降级过程幂等并记录完整切换过程。（代码：`resilience.py:14-104, 156-183`；MCP 接入：`research_agent.py:670-680`）

## P1：Bad Case 回流【DOCX-9】

40. 【√】【新增】收集用户纠正、点踩和明确拒绝结果。（代码：`bad_cases.py:100-106`；CLI：`research_agent.py:1088-1097`）
41. 【√】【新增】收集检索、引用、周报、记忆和 Agent 冲突案例。（代码：`bad_cases.py:15-23, 80-98`；自动回流：`research_agent.py:694-734`）
42. 【√】【新增】统一保存输入、执行轨迹、结果和失败阶段。（代码：`bad_cases.py:51-66, 80-98`）
43. 【√】【新增】按检索、记忆、规划、工具、协作和输出分类。（代码：`bad_cases.py:15-23, 80-87`）
44. 【√】【新增】写入前脱敏研究数据和个人信息。（代码：`bad_cases.py:33-48, 88-89`）
45. 【√】【新增】由人工审核是否加入正式案例库。（代码：`bad_cases.py:69-77, 108-126`；CLI：`research_agent.py:1098-1108`）
46. 【√】【新增】记录根因、修复版本和复测结果。（代码：`bad_cases.py:61-64, 128-146`）
47. 【√】【新增】支持按问题类型检索和重放案例。（代码：`bad_cases.py:148-177`；工具接入：`research_agent.py:793-798, 920-928`）

## P1：链路监控【DOCX-11】

48. 【√】【原有】Agent Loop 设置最大轮数和输出 Token 上限。（代码：`research_agent.py:970-1000, 1034-1039`）
49. 【√】【新增】为每次研究任务生成统一 trace_id。（代码：`observability.py:34-51, 85-102`；接入：`research_agent.py:970-979`）
50. 【√】【新增】记录主 Agent 和子 Agent 的输入、输出 Token。（代码：`observability.py:130-146`；主/子 Agent 接入：`research_agent.py:1006-1018`, `multi_agent.py:426-466, 547-625`）
51. 【√】【新增】记录模型、Embedding、RAG、MCP 和工具耗时。（代码：`observability.py:54-66, 113-158, 265-278`；工具接入：`research_agent.py:694-730`）
52. 【√】【新增】记录子任务状态、重试和失败原因。（代码：`multi_agent.py:425-470, 571-620`）
53. 【√】【新增】统计任务及研究周报成功率和组件失败率。（代码：`observability.py:212-232`）
54. 【√】【新增】计算单次任务费用和总执行时间。（代码：`observability.py:75-82, 130-146, 193-203`）
55. 【√】【新增】设置 Token、时间和费用预算。（代码：`observability.py:22-31, 75-82, 160-191`；配置：`.env.example:27-32`）
56. 【√】【新增】超过预算时暂停并请求人工决定。（代码：`observability.py:160-191`；接入：`research_agent.py:599-606, 985-990, 1079-1087`）
57. 【√】【新增】对比单 Agent 与多 Agent 的质量、成本和延迟。（代码：`observability.py:234-249`；工具接入：`research_agent.py:803-806, 932-934`）
58. 【√】【新增】生成单次任务 Trace 报告。（代码：`observability.py:205-210`；工具接入：`research_agent.py:799-806, 929-934`）

## 完成科研业务闭环

59. 【√】【原有】具备任务、RAG、记忆、Cron、审批、MCP 和子 Agent 组件。（代码：`research_agent.py:53-345, 348-517, 562-935`；记忆：`memory_system.py:44-273`；多 Agent：`multi_agent.py:31-626`）
60. 【√】【原有】通知必须经人工审批后写入本地 outbox。（代码：`research_agent.py:348-445, 751-756, 873-881`）
61. 【×】【新增】实现端到端研究周报工作流。
62. 【×】【新增】汇总论文、笔记、实验、任务和子 Agent 结果。
63. 【×】【新增】生成带证据来源的周报并由 Reviewer 审核。
64. 【×】【新增】选择真实 Zotero、GitHub 或学术 MCP Server。
65. 【×】【新增】接入一种真实通知渠道。
66. 【×】【新增】实现独立后台 Cron 服务和重启去重。
67. 【×】【新增】准备完整演示数据和演示脚本。

## 基础维护与明确边界

68. 【√】【原有】模型 API 临时错误支持基础指数退避重试。（代码：`research_agent.py:939-953`；服务降级重试：`resilience.py:44-68, 121-134`）
69. 【√】【原有】通知和非只读 MCP 操作具备人工审批门控。（代码：`research_agent.py:348-445, 806-840, 1067-1074`）
70. 【√】【原有】已有 41 项聚焦自动化测试。（测试：`test_research_agent.py:1-235`, `test_research_memory_multi_agent.py:1-156`, `test_research_resilience_arbitration.py:1-198`, `test_bad_cases_observability.py:1-115`）
71. 【×】【新增】增加外部调用超时、失败隔离和结构化日志。
72. 【×】【新增】增加真实模型、MCP 和 Elasticsearch 集成测试。
73. 【√】【原有】不深化通用混合规划框架【DOCX-1】。（范围决策，无对应实现代码）
74. 【√】【原有】不建设通用反思与代码自检框架【DOCX-3】。（范围决策，无对应实现代码）
75. 【√】【原有】不建设完整命令熔断系统【DOCX-4】。（范围决策，无对应实现代码）
76. 【√】【原有】不建设离线代码评测平台【DOCX-8】。（范围决策，无对应实现代码）
77. 【√】【原有】不承担定期回归平台建设【DOCX-10】。（范围决策，无对应实现代码）
78. 【√】【原有】不实现系统级命令沙箱和代码审计【DOCX-12】。（范围决策，无对应实现代码）
