package com.hyj.researchagent;

import com.fasterxml.jackson.core.type.TypeReference;
import com.hyj.researchagent.ResearchStores.ApprovalRequest;

import java.nio.file.Files;
import java.nio.file.Path;
import java.time.LocalDateTime;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;

/** 聚合科研组件并把内置/MCP 工具暴露给 Agent Loop。 */
public final class ResearchRuntime {
    private final Path workspace,dataDir;private final MemoryStore memory;private final ResearchStores.TaskStore tasks;
    private final ResearchStores.ApprovalStore approvals;private final ResearchStores.NotificationStore notifications;
    private final ResearchStores.CronScheduler cron;private final TraceStore traces;private final BadCaseStore badCases;
    private final Resilience.FallbackJournal fallback;private KnowledgeService.KnowledgeBase knowledge;
    private final Map<String,McpSupport.McpClient> mcpClients=new LinkedHashMap<>();private final Map<String,McpSupport.Tool> mcpTools=new LinkedHashMap<>();
    private MultiAgent.AgentTeam team;

    public ResearchRuntime(Path workspace){this(workspace,null);}
    public ResearchRuntime(Path workspace,KnowledgeService.EmbeddingModel embedding){
        this.workspace=workspace.toAbsolutePath().normalize();this.dataDir=this.workspace.resolve(".research-agent");
        try { Files.createDirectories(this.dataDir); }
        catch (java.io.IOException error) { throw new IllegalStateException("Cannot create research data directory", error); }
        this.approvals=new ResearchStores.ApprovalStore(dataDir);this.memory=new MemoryStore(dataDir);this.tasks=new ResearchStores.TaskStore(dataDir);
        this.notifications=new ResearchStores.NotificationStore(dataDir,approvals);this.cron=new ResearchStores.CronScheduler(dataDir);this.fallback=new Resilience.FallbackJournal(dataDir);
        this.traces=new TraceStore(dataDir,TraceStore.TraceBudget.fromEnvironment(),payload->approvals.requestApproval("override_trace_budget",payload).id(),approvals::approved);
        this.badCases=new BadCaseStore(dataDir,payload->approvals.requestApproval("review_bad_case",payload).id(),approvals::approved);
        this.knowledge=new KnowledgeService.LocalKnowledgeBase(dataDir,embedding==null?new KnowledgeService.LocalEmbeddingModel():embedding);
    }

    public void bindTeam(MultiAgent.AgentTeam team){this.team=team;}
    public void setKnowledge(KnowledgeService.KnowledgeBase knowledge){this.knowledge=knowledge;}

    /** 连接并注册一个 MCP Server 的工具。 */
    public String connectMcp(String name,McpSupport.Transport transport){
        if(transport==null)return "MCP server '"+name+"' is not configured";
        try{McpSupport.McpClient client=new McpSupport.McpClient(name,transport);List<McpSupport.Tool> tools=client.connect();mcpClients.put(client.name(),client);tools.forEach(tool->mcpTools.put(tool.exposedName(),tool));return "Discovered "+tools.size()+" tools from "+client.name();}
        catch(Exception error){return "Error connecting MCP: "+error;}
    }

    /** 从 .research-agent/mcp_servers.json 的 argv 配置启动真实 stdio MCP Server。 */
    public String connectConfiguredMcp(String name){
        Path config=dataDir.resolve("mcp_servers.json");if(!Files.exists(config))return "MCP server '"+name+"' is not configured";
        try{Map<String,Object> all=JsonFiles.read(config,new TypeReference<>(){},Map.of());Object raw=all.get(name);if(!(raw instanceof Map<?,?>))return "MCP server '"+name+"' is not configured";
            Map<String,Object> item=JsonFiles.map(raw);List<String> command=strings(item.get("command"));Path cwd=item.containsKey("cwd")?workspace.resolve(String.valueOf(item.get("cwd"))).normalize():workspace;
            Map<String,String> env=new LinkedHashMap<>();if(item.get("env") instanceof Map<?,?> values)values.forEach((k,v)->env.put(String.valueOf(k),String.valueOf(v)));
            return connectMcp(name,new McpSupport.StdioTransport(command,cwd,env));}catch(Exception error){return "Error connecting MCP: "+error;}
    }

    /** 返回模型可见工具 Schema；子 Agent 可传白名单形成受限工具池。 */
    public List<Map<String,Object>> assembleToolPool(Set<String> allowed){
        List<Map<String,Object>> tools=new ArrayList<>(builtinTools());for(McpSupport.Tool tool:mcpTools.values())tools.add(Map.of("name",tool.exposedName(),"description",tool.description(),"input_schema",tool.inputSchema()));
        return allowed==null?tools:tools.stream().filter(tool->allowed.contains(String.valueOf(tool.get("name")))).toList();
    }

    /** 执行工具并记录 span；失败自动形成待审核 Bad Case 候选。 */
    public String execute(String name,Map<String,Object> args){String trace=traces.currentTraceId();long started=System.nanoTime();String output;
        try{output=executeInternal(name,args);}catch(Exception error){output="Error: "+error.getClass().getSimpleName()+": "+error.getMessage();}
        boolean failed=output.startsWith("Error");if(trace!=null)traces.recordSpan(trace,component(name),name,failed?"failed":"success",(System.nanoTime()-started)/1_000_000d,failed?output:"",Map.of());
        if(failed)badCases.collect(caseType(name),args,List.of(Map.of("tool",name,"args",args)),output,name,"system",null);return output;}

    private String executeInternal(String name,Map<String,Object> args)throws Exception{
        if(mcpTools.containsKey(name))return executeMcp(name,args);
        return switch(name){
            case "create_task"->tasks.createTask(string(args,"subject"),string(args,"description",""),strings(args.get("blocked_by"))).id();
            case "claim_task"->tasks.claimTask(string(args,"task_id"),string(args,"owner","agent"));
            case "complete_task"->tasks.completeTask(string(args,"task_id"));
            case "list_tasks"->JsonFiles.toJson(tasks.listTasks());
            case "remember"->memory.remember(string(args,"content"),string(args,"category","fact"));
            case "recall"->JsonFiles.toJson(memory.recall(string(args,"query",""),integer(args,"limit",10)));
            case "save_working"->JsonFiles.toJson(memory.saveWorking(string(args,"task_id"),strings(args.get("plan")),null,null));
            case "add_evidence"->JsonFiles.toJson(memory.addEvidence(string(args,"task_id"),string(args,"content"),string(args,"source"),decimal(args,"confidence",0.5)));
            case "update_progress"->JsonFiles.toJson(memory.updateProgress(string(args,"task_id"),string(args,"step"),string(args,"status")));
            case "search_knowledge"->JsonFiles.toJson(knowledge.search(string(args,"query"),integer(args,"limit",5)));
            case "add_document"->knowledge.addDocument(string(args,"title"),string(args,"content"),string(args,"source","manual"),integer(args,"chunk_size",800));
            case "schedule_cron"->cron.scheduleJob(string(args,"cron"),string(args,"prompt"),true).id();
            case "list_crons"->JsonFiles.toJson(cron.listJobs());
            case "due_cron"->JsonFiles.toJson(cron.dueJobs(LocalDateTime.now()));
            case "request_approval"->approvals.requestApproval(string(args,"action"),args.get("payload") instanceof Map<?,?>?JsonFiles.map(args.get("payload")):Map.of()).id();
            case "list_approvals"->JsonFiles.toJson(approvals.listApprovals());
            case "request_notification"->notifications.requestNotification(string(args,"channel"),string(args,"recipient"),string(args,"content")).id();
            case "deliver_notification"->notifications.deliverNotification(string(args,"notification_id"));
            case "list_notifications"->JsonFiles.toJson(notifications.listNotifications());
            case "connect_mcp"->connectConfiguredMcp(string(args,"name"));
            case "spawn_subagent"->team==null?"Error: Agent team is not configured":team.spawnSubagent(string(args,"name"),string(args,"role"),string(args,"prompt"),null);
            case "collect_subagent_results"->team==null?"Error: Agent team is not configured":team.collectResults();
            case "cancel_subagent"->team==null?"Error: Agent team is not configured":team.cancelSubagent(string(args,"name"));
            case "reassign_subagent"->team==null?"Error: Agent team is not configured":team.reassignFailed(string(args,"name"),string(args,"replacement",null));
            case "run_research_workflow"->team==null?"Error: Agent team is not configured":JsonFiles.toJson(team.runWorkflow(string(args,"research_question",string(args,"question",""))));
            case "resume_workflow"->team==null?"Error: Agent team is not configured":JsonFiles.toJson(team.resumeWorkflow(string(args,"workflow_id")));
            case "rollback_workflow"->team==null?"Error: Agent team is not configured":JsonFiles.toJson(team.rollbackWorkflow(string(args,"workflow_id"),string(args,"checkpoint_id")));
            case "list_workflow_checkpoints"->team==null?"Error: Agent team is not configured":JsonFiles.toJson(team.store.listSnapshots(string(args,"workflow_id")));
            case "list_conclusion_conflicts"->team==null?"Error: Agent team is not configured":JsonFiles.toJson(team.arbitrator.conflicts(string(args,"question")));
            case "arbitrate_conclusions"->team==null?"Error: Agent team is not configured":JsonFiles.toJson(team.arbitrator.arbitrate(strings(args.get("conclusion_ids"))));
            case "promote_conclusion"->team==null?"Error: Agent team is not configured":team.arbitrator.promote(string(args,"decision_id"),memory);
            case "record_bad_case"->badCases.collect(string(args,"case_type"),args.get("task_input"),trajectory(args.get("trajectory")),args.get("result"),string(args,"failure_stage"),string(args,"source","system"),string(args,"category",null)).id();
            case "search_bad_cases"->JsonFiles.toJson(badCases.search(string(args,"query",""),string(args,"category",null),string(args,"case_type",null),booleanValue(args,"accepted_only",true)));
            case "compare_agent_modes"->JsonFiles.toJson(traces.compareModes());
            case "replay_bad_case"->JsonFiles.toJson(badCases.replay(string(args,"case_id"),null));
            case "record_case_remediation"->JsonFiles.toJson(badCases.recordRemediation(string(args,"case_id"),string(args,"root_cause"),string(args,"fixed_version"),string(args,"retest_result")));
            case "trace_report","get_trace_report"->JsonFiles.toJson(traces.report(string(args,"trace_id")));
            case "trace_metrics","get_monitoring_metrics"->JsonFiles.toJson(traces.metrics());
            case "submit_conclusion"->team==null?"Error: Agent team is not configured":submitConclusion(args);
            default->"Error: unknown tool '"+name+"'";
        };}

    private String executeMcp(String name,Map<String,Object> args)throws Exception{McpSupport.Tool tool=mcpTools.get(name);McpSupport.McpClient client=mcpClients.get(tool.server());
        if(!tool.readOnly()){ApprovalRequest approval=approvals.requestApproval("mcp_tool",Map.of("tool",name,"args",args));return "pending approval "+approval.id();}
        return callMcpFallback(client,name,args);}

    /** 只执行已批准的 MCP 副作用调用，并在成功后把审批置为 executed。 */
    public String executeApprovedAction(String approvalId){ApprovalRequest approval=approvals.getApproval(approvalId);if(approval==null||!approval.status().equals("approved"))return "Error: approval is not approved";
        try{String tool=String.valueOf(approval.payload().get("tool"));Map<String,Object> args=JsonFiles.map(approval.payload().get("args"));McpSupport.Tool meta=mcpTools.get(tool);String result=mcpClients.get(meta.server()).call(tool,args);approvals.markExecuted(approvalId);return result;}
        catch(Exception error){return "Error: "+error;}}

    private String callMcpFallback(McpSupport.McpClient primary,String tool,Map<String,Object> args)throws Exception{
        List<Resilience.Provider<String>> providers=new ArrayList<>();providers.add(new Resilience.Provider<>(primary.name(),()->primary.call(tool,args)));
        for(String backup:fallbackServers(primary.name())){McpSupport.McpClient client=mcpClients.get(McpSupport.normalize(backup));if(client!=null){String backupTool="mcp__"+client.name()+"__"+tool.substring(tool.lastIndexOf("__")+2);if(client.tool(backupTool)!=null)providers.add(new Resilience.Provider<>(client.name(),()->client.call(backupTool,args)));}}
        String id=Resilience.operationId("mcp",Map.of("tool",tool,"args",args));try{return fallback.execute("mcp",id,providers,false);}catch(Exception error){String pending=fallback.defer("mcp",Map.of("tool",tool,"args",args),error.toString());return "MCP unavailable; pending task "+pending;}}

    private List<String> fallbackServers(String server){Path config=dataDir.resolve("mcp_servers.json");if(!Files.exists(config))return List.of();Map<String,Object> all=JsonFiles.read(config,new TypeReference<>(){},Map.of());Object value=all.get(server);if(!(value instanceof Map<?,?>))return List.of();return strings(JsonFiles.map(value).get("fallback_servers"));}
    private String submitConclusion(Map<String,Object> args){List<MultiAgent.Evidence> evidence=new ArrayList<>();if(args.get("evidence") instanceof List<?> list)for(Object raw:list){Map<String,Object> item=JsonFiles.map(raw);evidence.add(new MultiAgent.Evidence(string(item,"content",""),string(item,"source"),decimal(item,"confidence",0.5)));}
        return team.arbitrator.submit(string(args,"question"),string(args,"claim"),string(args,"agent","agent"),evidence,decimal(args,"confidence",0.5)).id();}
    public String resolveConflict(String decision,String selected){try{return JsonFiles.toJson(team.arbitrator.resolveHuman(decision,selected));}catch(SecurityException error){return "Error: human approval is required";}}

    private static List<Map<String,Object>> builtinTools(){
        String[] names={"add_document","search_knowledge","remember","recall","save_working","add_evidence","update_progress","create_task","list_tasks","claim_task","complete_task","request_approval","list_approvals","request_notification","deliver_notification","list_notifications","schedule_cron","list_crons","connect_mcp","spawn_subagent","collect_subagent_results","cancel_subagent","reassign_subagent","run_research_workflow","submit_conclusion","list_conclusion_conflicts","arbitrate_conclusions","promote_conclusion","list_workflow_checkpoints","rollback_workflow","resume_workflow","record_bad_case","search_bad_cases","replay_bad_case","record_case_remediation","get_trace_report","get_monitoring_metrics","compare_agent_modes"};
        return java.util.Arrays.stream(names).map(ResearchRuntime::tool).toList();
    }
    private static Map<String,Object> tool(String name){List<String> required=requiredFields(name);Map<String,Object> properties=new LinkedHashMap<>();
        for(String field:allFields(name))properties.put(field,property(field));
        return Map.of("name",name,"description","Research tool: "+name,"input_schema",Map.of("type","object","properties",properties,"required",required));}
    private static Map<String,Object> property(String field){String type=field.endsWith("_ids")||Set.of("evidence","trajectory","plan","blocked_by").contains(field)?"array":
            Set.of("payload","task_input","result").contains(field)?"object":Set.of("confidence").contains(field)?"number":Set.of("limit","chunk_size").contains(field)?"integer":field.equals("accepted_only")?"boolean":"string";
        return type.equals("array")?Map.of("type","array","items",Map.of("type",field.equals("evidence")||field.equals("trajectory")?"object":"string")):Map.of("type",type);}
    private static List<String> allFields(String name){return switch(name){
        case"add_document"->List.of("title","content","source","chunk_size");case"search_knowledge"->List.of("query","limit");case"remember"->List.of("content","category");case"recall"->List.of("query","limit");
        case"save_working"->List.of("task_id","plan");case"add_evidence"->List.of("task_id","content","source","confidence");case"update_progress"->List.of("task_id","step","status");case"create_task"->List.of("subject","description","blocked_by");
        case"claim_task"->List.of("task_id","owner");case"complete_task"->List.of("task_id");case"request_approval"->List.of("action","payload");case"request_notification"->List.of("channel","recipient","content");
        case"deliver_notification"->List.of("notification_id");case"schedule_cron"->List.of("cron","prompt");case"connect_mcp"->List.of("name");case"spawn_subagent"->List.of("name","role","prompt");
        case"cancel_subagent"->List.of("name");case"reassign_subagent"->List.of("name","replacement");case"run_research_workflow"->List.of("research_question");case"submit_conclusion"->List.of("question","claim","agent","evidence","confidence");
        case"list_conclusion_conflicts"->List.of("question");case"arbitrate_conclusions"->List.of("conclusion_ids");case"promote_conclusion"->List.of("decision_id");case"list_workflow_checkpoints","resume_workflow"->List.of("workflow_id");
        case"rollback_workflow"->List.of("workflow_id","checkpoint_id");case"record_bad_case"->List.of("case_type","task_input","trajectory","result","failure_stage","source","category");case"search_bad_cases"->List.of("query","category","case_type","accepted_only");
        case"replay_bad_case"->List.of("case_id");case"record_case_remediation"->List.of("case_id","root_cause","fixed_version","retest_result");case"get_trace_report"->List.of("trace_id");default->List.of();};}
    private static List<String> requiredFields(String name){return switch(name){
        case"add_document"->List.of("title","content");case"search_knowledge"->List.of("query");case"remember"->List.of("content");case"save_working"->List.of("task_id");
        case"add_evidence"->List.of("task_id","content","source");case"update_progress"->List.of("task_id","step","status");case"create_task"->List.of("subject");
        case"claim_task","complete_task"->List.of("task_id");case"request_approval"->List.of("action","payload");case"request_notification"->List.of("channel","recipient","content");
        case"deliver_notification"->List.of("notification_id");case"schedule_cron"->List.of("cron","prompt");case"connect_mcp"->List.of("name");case"spawn_subagent"->List.of("name","role","prompt");
        case"cancel_subagent","reassign_subagent"->List.of("name");case"run_research_workflow"->List.of("research_question");case"submit_conclusion"->List.of("question","claim","agent","evidence","confidence");
        case"list_conclusion_conflicts"->List.of("question");case"arbitrate_conclusions"->List.of("conclusion_ids");case"promote_conclusion"->List.of("decision_id");
        case"list_workflow_checkpoints","resume_workflow"->List.of("workflow_id");case"rollback_workflow"->List.of("workflow_id","checkpoint_id");
        case"record_bad_case"->List.of("case_type","task_input","result","failure_stage");case"replay_bad_case"->List.of("case_id");
        case"record_case_remediation"->List.of("case_id","root_cause","fixed_version","retest_result");case"get_trace_report"->List.of("trace_id");default->List.of();};}
    private static String component(String name){if(name.startsWith("mcp__"))return "mcp";if(name.contains("knowledge")||name.equals("add_document"))return "rag";return "tool";}
    private static String caseType(String name){if(name.contains("knowledge"))return "retrieval";if(name.contains("memory")||Set.of("remember","recall").contains(name))return "memory";if(name.contains("agent")||name.contains("workflow"))return "collaboration";return "tool";}
    private static String string(Map<String,Object> args,String key){Object v=args.get(key);if(v==null)throw new IllegalArgumentException("Missing "+key);return String.valueOf(v);}private static String string(Map<String,Object> args,String key,String fallback){return args.containsKey(key)?String.valueOf(args.get(key)):fallback;}
    private static int integer(Map<String,Object> args,String key,int fallback){return args.get(key) instanceof Number n?n.intValue():fallback;}private static double decimal(Map<String,Object> args,String key,double fallback){return args.get(key) instanceof Number n?n.doubleValue():fallback;}
    private static List<String> strings(Object value){if(!(value instanceof List<?> list))return List.of();return list.stream().map(String::valueOf).toList();}
    private static List<Map<String,Object>> trajectory(Object value){if(!(value instanceof List<?> list))return List.of();return list.stream().map(JsonFiles::map).toList();}
    private static boolean booleanValue(Map<String,Object> args,String key,boolean fallback){return args.get(key) instanceof Boolean value?value:fallback;}

    public Path workspace(){return workspace;}public Path dataDir(){return dataDir;}public MemoryStore memory(){return memory;}public ResearchStores.TaskStore tasks(){return tasks;}
    public ResearchStores.ApprovalStore approvals(){return approvals;}public ResearchStores.NotificationStore notifications(){return notifications;}public ResearchStores.CronScheduler cron(){return cron;}
    public TraceStore traces(){return traces;}public BadCaseStore badCases(){return badCases;}public Map<String,McpSupport.McpClient> mcpClients(){return mcpClients;}
}
