package com.hyj.researchagent;

import com.fasterxml.jackson.core.type.TypeReference;

import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Duration;
import java.time.Instant;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ConcurrentLinkedQueue;
import java.util.concurrent.atomic.AtomicBoolean;

/** Planner、Researcher、Writer、Reviewer 多 Agent 协议、仲裁和可恢复工作流。 */
public final class MultiAgent {
    private MultiAgent() {}

    public record RoleSpec(String name,String purpose,List<String> inputFields,List<String> outputFields,Set<String> allowedTools) {}
    public static final Map<String,RoleSpec> ROLES=Map.of(
            "planner",new RoleSpec("planner","Plan the research task",List.of("research_question"),List.of("plan","success_criteria"),Set.of("list_tasks","create_task","recall")),
            "researcher",new RoleSpec("researcher","Collect sourced evidence",List.of("research_question","plan"),List.of("evidence","confidence"),Set.of("search_knowledge","add_evidence","recall")),
            "writer",new RoleSpec("writer","Draft from evidence",List.of("research_question","evidence"),List.of("draft","citations"),Set.of("search_knowledge","recall")),
            "reviewer",new RoleSpec("reviewer","Review evidence and final answer",List.of("research_question","draft","citations"),List.of("approved","issues","final"),Set.of("search_knowledge","recall","submit_conclusion")));
    private static final Map<String,String> ROLE_ALIASES=Map.of("文献分析","researcher","规划","planner","写作","writer","审核","reviewer");

    public record Evidence(String content,String source,double confidence) {}
    public record AgentResult(String agent,String role,Map<String,Object> payload,List<Evidence> evidence,double confidence,String content) {}

    /** 验证角色输出是单个 JSON 对象并包含该角色要求的字段。 */
    public static AgentResult parseRoleResult(String agent,String role,String text){
        RoleSpec spec=ROLES.get(role);if(spec==null)throw new IllegalArgumentException("Unknown role: "+role);
        try{
            Map<String,Object> payload=JsonFiles.JSON.readValue(text,new TypeReference<>(){});
            List<String> missing=spec.outputFields().stream().filter(field->!payload.containsKey(field)).toList();
            if(!missing.isEmpty())throw new IllegalArgumentException("missing fields: "+missing);
            List<Evidence> evidence=new ArrayList<>();Object raw=payload.get("evidence");
            if(raw instanceof List<?> list)for(Object item:list){Map<String,Object> map=JsonFiles.map(item);String source=String.valueOf(map.getOrDefault("source",""));
                if(source.isBlank())throw new IllegalArgumentException("Evidence requires source");evidence.add(new Evidence(String.valueOf(map.getOrDefault("content","")),source,number(map.get("confidence"),0.5)));}
            return new AgentResult(agent,role,payload,evidence,number(payload.get("confidence"),1.0),text);
        }catch(com.fasterxml.jackson.core.JsonProcessingException error){throw new IllegalArgumentException("Role output must be JSON",error);}
    }

    public static final class AgentRecord {
        public String name,role,prompt,taskId,status="pending",result="",error="",createdAt=JsonFiles.now(),startedAt="",finishedAt="";public int attempts;
        public AgentRecord(){}
        AgentRecord(String name,String role,String prompt,String taskId){this.name=name;this.role=role;this.prompt=prompt;this.taskId=taskId;}
    }
    public static final class WorkflowTask {
        public String id,role,status="pending",result="";public List<String> dependsOn=new ArrayList<>();
        public WorkflowTask(){} WorkflowTask(String id,String role,List<String> depends){this.id=id;this.role=role;this.dependsOn=new ArrayList<>(depends);}
    }
    public static final class WorkflowState {
        public String id,researchQuestion,status,createdAt=JsonFiles.now(),updatedAt=JsonFiles.now();
        public List<WorkflowTask> tasks=new ArrayList<>();public Map<String,String> results=new LinkedHashMap<>();
        public WorkflowState(){} WorkflowState(String id,String question,String status,List<WorkflowTask> tasks){this.id=id;this.researchQuestion=question;this.status=status;this.tasks=tasks;}
    }

    /** 持久化 Agent/工作流，追加事件，并为局部回滚保存快照。 */
    public static final class WorkflowStore {
        private final Path agents,workflows,events,snapshots;
        public WorkflowStore(Path dataDir){agents=dataDir.resolve("agents.json");workflows=dataDir.resolve("workflows.json");events=dataDir.resolve("workflow_events.jsonl");snapshots=dataDir.resolve("workflow_snapshots.json");}
        public synchronized void saveAgent(AgentRecord record){Map<String,AgentRecord> all=JsonFiles.read(agents,new TypeReference<>(){},new LinkedHashMap<>());all.put(record.name,record);JsonFiles.write(agents,all);}
        public AgentRecord loadAgent(String name){Map<String,Object> all=JsonFiles.read(agents,new TypeReference<>(){},new LinkedHashMap<>());return all.containsKey(name)?JsonFiles.JSON.convertValue(all.get(name),AgentRecord.class):null;}
        public synchronized void saveWorkflow(WorkflowState state){Map<String,WorkflowState> all=JsonFiles.read(workflows,new TypeReference<>(){},new LinkedHashMap<>());all.put(state.id,state);JsonFiles.write(workflows,all);}
        public WorkflowState loadWorkflow(String id){Map<String,Object> all=JsonFiles.read(workflows,new TypeReference<>(){},new LinkedHashMap<>());return all.containsKey(id)?JsonFiles.JSON.convertValue(all.get(id),WorkflowState.class):null;}
        public void event(String type,Map<String,Object> payload){JsonFiles.appendJsonLine(events,Map.of("time",JsonFiles.now(),"type",type,"payload",payload));}
        public synchronized String snapshot(WorkflowState state,String label){String id=JsonFiles.id("checkpoint");Map<String,Object> all=JsonFiles.read(snapshots,new TypeReference<>(){},new LinkedHashMap<>());
            List<Map<String,Object>> list=new ArrayList<>((List<Map<String,Object>>)all.getOrDefault(state.id,new ArrayList<>()));list.add(Map.of("id",id,"label",label,"time",JsonFiles.now(),"state",JsonFiles.map(state)));all.put(state.id,list);JsonFiles.write(snapshots,all);return id;}
        public WorkflowState restore(String workflowId,String checkpointId){for(Map<String,Object> item:listSnapshots(workflowId))if(checkpointId.equals(item.get("id")))return JsonFiles.JSON.convertValue(item.get("state"),WorkflowState.class);throw new IllegalArgumentException("Unknown checkpoint");}
        public List<Map<String,Object>> listSnapshots(String id){Map<String,Object> all=JsonFiles.read(snapshots,new TypeReference<>(){},new LinkedHashMap<>());return (List<Map<String,Object>>)all.getOrDefault(id,List.of());}
    }

    public record Conclusion(String id,String question,String claim,String agent,List<Evidence> evidence,double confidence,String status,String createdAt) {}
    public record ArbitrationDecision(String id,String question,List<String> conclusionIds,String selectedId,String status,String reason,String approvalId) {}

    /** 按证据可信度自动仲裁；分差不足时强制转人工审批。 */
    public static final class ConflictArbitrator {
        private final Path conclusions,decisions;private final BadCaseStore.ApprovalRequester requester;private final BadCaseStore.ApprovalChecker checker;private final double margin;
        public ConflictArbitrator(Path dataDir,BadCaseStore.ApprovalRequester requester,BadCaseStore.ApprovalChecker checker){this(dataDir,requester,checker,0.1);}
        public ConflictArbitrator(Path dataDir,BadCaseStore.ApprovalRequester requester,BadCaseStore.ApprovalChecker checker,double margin){
            conclusions=dataDir.resolve("conclusions.json");decisions=dataDir.resolve("arbitration_decisions.json");this.requester=requester;this.checker=checker;this.margin=margin;}
        public synchronized Conclusion submit(String question,String claim,String agent,List<Evidence> evidence,double confidence){
            if(evidence==null||evidence.isEmpty()||evidence.stream().anyMatch(e->e.source()==null||e.source().isBlank()))throw new IllegalArgumentException("Conclusion requires sourced evidence");
            Conclusion value=new Conclusion(JsonFiles.id("conclusion"),question,claim,agent,List.copyOf(evidence),confidence,"pending",JsonFiles.now());Map<String,Conclusion> all=readConclusions();all.put(value.id(),value);JsonFiles.write(conclusions,all);return value;}
        public List<Conclusion> conflicts(String question){List<Conclusion> values=readConclusions().values().stream().filter(c->normalize(c.question()).equals(normalize(question))).toList();return values.stream().map(Conclusion::claim).map(ConflictArbitrator::normalize).distinct().count()>1?values:List.of();}
        public synchronized ArbitrationDecision arbitrate(List<String> ids){
            Map<String,Conclusion> all=readConclusions();List<Conclusion> values=ids.stream().distinct().map(all::get).filter(java.util.Objects::nonNull).sorted(Comparator.comparingDouble(Conclusion::confidence).thenComparingInt(c->c.evidence().size()).reversed()).toList();
            if(values.size()!=ids.stream().distinct().count()||values.size()<2)throw new IllegalArgumentException("At least two existing conclusions are required");
            if(values.stream().map(c->normalize(c.question())).distinct().count()!=1)throw new IllegalArgumentException("Conclusions must answer the same question");
            double difference=values.get(0).confidence()-values.get(1).confidence();boolean automatic=difference>=margin;String selected=automatic?values.get(0).id():null;
            String decisionId=JsonFiles.id("decision");String approval=!automatic&&requester!=null?requester.request(Map.of("decision_id",decisionId,"conclusions",ids)):null;
            ArbitrationDecision value=new ArbitrationDecision(decisionId,values.get(0).question(),List.copyOf(ids),selected,automatic?"approved":"human_required",automatic?"Lead selected higher-confidence evidence":"Confidence margin requires human review",approval);
            saveDecision(value);if(automatic)apply(value);return value;}
        public synchronized ArbitrationDecision resolveHuman(String id,String selected){ArbitrationDecision old=getDecision(id);if(old==null||!old.conclusionIds().contains(selected))throw new IllegalArgumentException("Invalid decision or selected conclusion");
            if(old.approvalId()==null||checker==null||!checker.approved(old.approvalId()))throw new SecurityException("Human approval is required");ArbitrationDecision value=new ArbitrationDecision(old.id(),old.question(),old.conclusionIds(),selected,"approved","Human selected the supported conclusion",old.approvalId());saveDecision(value);apply(value);return value;}
        public ArbitrationDecision getDecision(String id){Map<String,Object> all=JsonFiles.read(decisions,new TypeReference<>(){},new LinkedHashMap<>());return all.containsKey(id)?JsonFiles.JSON.convertValue(all.get(id),ArbitrationDecision.class):null;}
        public String promote(String decisionId,MemoryStore memory){ArbitrationDecision decision=getDecision(decisionId);if(decision==null||!decision.status().equals("approved")||decision.selectedId()==null)throw new IllegalArgumentException("Only an approved conclusion can enter long-term memory");Conclusion item=readConclusions().get(decision.selectedId());return memory.remember(item.claim(),"confirmed_conclusion","reviewer",item.confidence(),true,"lead",true);}
        private void apply(ArbitrationDecision decision){Map<String,Conclusion> all=readConclusions();for(String id:decision.conclusionIds()){Conclusion old=all.get(id);all.put(id,new Conclusion(old.id(),old.question(),old.claim(),old.agent(),old.evidence(),old.confidence(),id.equals(decision.selectedId())?"accepted":"rejected",old.createdAt()));}JsonFiles.write(conclusions,all);}
        private void saveDecision(ArbitrationDecision value){Map<String,ArbitrationDecision> all=JsonFiles.read(decisions,new TypeReference<>(){},new LinkedHashMap<>());all.put(value.id(),value);JsonFiles.write(decisions,all);}
        private Map<String,Conclusion> readConclusions(){return JsonFiles.read(conclusions,new TypeReference<>(){},new LinkedHashMap<>());}
        private static String normalize(String value){return value.toLowerCase(Locale.ROOT).trim().replaceAll("\\s+"," ");}
    }

    /** Lead 与子 Agent 共享的线程安全收件箱。 */
    public static final class MessageBus {
        private final Map<String,ConcurrentLinkedQueue<Map<String,Object>>> inboxes=new ConcurrentHashMap<>();
        public void send(String from,String to,String content,String type,String taskId){inboxes.computeIfAbsent(to,k->new ConcurrentLinkedQueue<>()).add(Map.of("id",JsonFiles.id("msg"),"time",JsonFiles.now(),"from",from,"to",to,"type",type,"task_id",taskId,"content",content));}
        public List<Map<String,Object>> readInbox(String name){ConcurrentLinkedQueue<Map<String,Object>> queue=inboxes.computeIfAbsent(name,k->new ConcurrentLinkedQueue<>());List<Map<String,Object>> result=new ArrayList<>();Map<String,Object> item;while((item=queue.poll())!=null)result.add(item);return result;}
    }

    @FunctionalInterface public interface AgentRunner {String run(String role,String prompt,String sessionId,String traceId) throws Exception;}

    /** 有界后台子 Agent 与 Planner→Researcher→Writer→Reviewer 工作流。 */
    public static final class AgentTeam {
        private final ResearchRuntime runtime;private AgentRunner runner;private final int maxConcurrency;private final double timeoutSeconds;
        public final MessageBus bus=new MessageBus();public final WorkflowStore store;public final ConflictArbitrator arbitrator;
        private final Map<String,Thread> threads=new ConcurrentHashMap<>();private final Map<String,AgentRecord> records=new ConcurrentHashMap<>();private final Map<String,AtomicBoolean> cancel=new ConcurrentHashMap<>();
        public AgentTeam(ResearchRuntime runtime,AgentRunner runner){this(runtime,runner,4,60);}
        public AgentTeam(ResearchRuntime runtime,AgentRunner runner,int maxConcurrency,double timeoutSeconds){this.runtime=runtime;this.runner=runner;this.maxConcurrency=maxConcurrency;this.timeoutSeconds=timeoutSeconds;this.store=new WorkflowStore(runtime.dataDir());
            this.arbitrator=new ConflictArbitrator(runtime.dataDir(),payload->runtime.approvals().requestApproval("resolve_agent_conflict",payload).id(),runtime.approvals()::approved);}
        public void setRunner(AgentRunner runner){this.runner=runner;}

        /** 启动受限角色线程；角色工具表中没有 spawn_subagent，因此不能递归创建。 */
        public synchronized String spawnSubagent(String name,String role,String prompt,String taskId){String safe=normalizeName(name);String key=ROLE_ALIASES.getOrDefault(role.toLowerCase(),role.toLowerCase());if(!ROLES.containsKey(key))return "Error: unknown role '"+role+"'";
            long active=threads.values().stream().filter(Thread::isAlive).count();if(active>=maxConcurrency)return "Error: maximum "+maxConcurrency+" concurrent subagents reached";if(threads.containsKey(safe)&&threads.get(safe).isAlive())return "Subagent '"+safe+"' already exists";
            AgentRecord record=new AgentRecord(safe,key,prompt,taskId==null?JsonFiles.id("task"):taskId);records.put(safe,record);cancel.put(safe,new AtomicBoolean());store.saveAgent(record);
            Thread thread=new Thread(()->runAgent(record),"agent-"+safe);thread.setDaemon(true);threads.put(safe,thread);thread.start();return "Subagent '"+safe+"' spawned as "+key;}
        private void runAgent(AgentRecord record){String trace=runtime.traces().startTrace("subtask","multi",null);long started=System.nanoTime();record.status="running";record.startedAt=JsonFiles.now();store.saveAgent(record);store.event("agent_started",Map.of("agent",record.name,"task_id",record.taskId));
            try{if(cancel.get(record.name).get())throw new IllegalStateException("cancelled");String result=runner.run(record.role,record.prompt,"agent-"+record.name,trace);if(cancel.get(record.name).get())throw new IllegalStateException("cancelled");record.status="completed";record.result=result;bus.send(record.name,"lead",result,"result",record.taskId);}
            catch(Exception error){record.status=cancel.get(record.name).get()?"cancelled":"failed";record.error=error.toString();bus.send(record.name,"lead","Subagent error: "+error,record.status,record.taskId);}
            finally{record.finishedAt=JsonFiles.now();store.saveAgent(record);runtime.traces().recordSpan(trace,"subtask",record.name,record.status.equals("completed")?"success":"failed",(System.nanoTime()-started)/1_000_000d,record.error,Map.of("attempts",record.attempts,"task_id",record.taskId));runtime.traces().finishTrace(trace,record.status,record.error,record.status.equals("completed")?1.0:0.0);store.event("agent_finished",Map.of("agent",record.name,"status",record.status));}}
        public String cancelSubagent(String name){AtomicBoolean flag=cancel.get(name);if(flag==null)return "Subagent '"+name+"' not found";flag.set(true);AgentRecord record=records.get(name);record.status="cancelled";record.finishedAt=JsonFiles.now();store.saveAgent(record);return "Subagent '"+name+"' cancellation requested";}
        public List<String> checkTimeouts(){List<String> result=new ArrayList<>();Instant now=Instant.now();for(AgentRecord record:records.values()){Thread thread=threads.get(record.name);if(!record.status.equals("running")||thread==null||!thread.isAlive())continue;if(Duration.between(Instant.parse(record.startedAt),now).toMillis()/1000d>timeoutSeconds){cancel.get(record.name).set(true);record.status="timed_out";record.error="timeout";record.finishedAt=JsonFiles.now();store.saveAgent(record);result.add(record.name);}}return result;}
        public String reassignFailed(String name,String replacement){AgentRecord old=records.getOrDefault(name,store.loadAgent(name));if(old==null||!Set.of("failed","timed_out","cancelled").contains(old.status))return "Error: subagent '"+name+"' has no failed task to reassign";String next=replacement==null?name+"-retry-"+(old.attempts+1):replacement;String result=spawnSubagent(next,old.role,old.prompt,old.taskId);if(result.contains("spawned")){records.get(next).attempts=old.attempts+1;store.saveAgent(records.get(next));}return result;}
        public String collectResults(){checkTimeouts();List<Map<String,Object>> messages=bus.readInbox("lead");return messages.isEmpty()?"No subagent results yet":JsonFiles.toJson(messages);}
        public Thread thread(String name){return threads.get(name);} public AgentRecord record(String name){return store.loadAgent(name);}

        public WorkflowState runWorkflow(String question){List<WorkflowTask> tasks=new ArrayList<>();List<String> previous=List.of();for(String role:List.of("planner","researcher","writer","reviewer")){WorkflowTask task=new WorkflowTask(JsonFiles.id("step"),role,previous);tasks.add(task);previous=List.of(task.id);}WorkflowState state=new WorkflowState(JsonFiles.id("workflow"),question,"running",tasks);store.saveWorkflow(state);runtime.memory().saveWorking(state.id,tasks.stream().map(t->t.role).toList(),List.of(),Map.of());return continueWorkflow(state);}
        public WorkflowState resumeWorkflow(String id){WorkflowState state=store.loadWorkflow(id);if(state==null)throw new IllegalArgumentException("Unknown workflow: "+id);if(state.status.equals("completed"))return state;state.status="running";return continueWorkflow(state);}
        public WorkflowState rollbackWorkflow(String id,String checkpoint){WorkflowState state=store.restore(id,checkpoint);state.status="paused";store.saveWorkflow(state);return state;}

        /** 每个角色失败可重试一次；仍失败则恢复该步骤前快照并暂停，之后可续跑。 */
        private WorkflowState continueWorkflow(WorkflowState state){String trace=runtime.traces().startTrace("research_task","multi",state.id);Map<String,Object> context=new LinkedHashMap<>();context.put("research_question",state.researchQuestion);state.results.forEach((k,v)->{try{context.putAll(JsonFiles.JSON.readValue(v,new TypeReference<Map<String,Object>>(){}));}catch(Exception e){context.put(k,v);}});
            for(WorkflowTask task:state.tasks){if(task.status.equals("completed"))continue;String checkpoint=store.snapshot(state,"before-"+task.role);task.status="running";store.saveWorkflow(state);RoleSpec spec=ROLES.get(task.role);List<String> missing=spec.inputFields().stream().filter(f->!context.containsKey(f)).toList();if(!missing.isEmpty()){task.status="failed";state.status="failed";task.result="missing role inputs: "+missing;store.saveWorkflow(state);runtime.traces().finishTrace(trace,"failed",task.result,0.0);return state;}
                Exception last=null;for(int attempt=1;attempt<=2;attempt++){long started=System.nanoTime();try{String output=runner.run(task.role,JsonFiles.toJson(context),"workflow-"+state.id+"-"+task.role+"-"+attempt,trace);AgentResult result=parseRoleResult(task.role,task.role,output);task.status="completed";task.result=result.content();state.results.put(task.role,result.content());context.putAll(result.payload());runtime.memory().updateProgress(state.id,task.role,"completed");for(Evidence evidence:result.evidence())runtime.memory().addEvidence(state.id,evidence.content(),evidence.source(),evidence.confidence());store.event("workflow_step_completed",Map.of("workflow",state.id,"role",task.role,"attempt",attempt));runtime.traces().recordSpan(trace,"subtask",task.role,"success",(System.nanoTime()-started)/1_000_000d,"",Map.of("attempt",attempt));last=null;break;}catch(Exception error){last=error;runtime.traces().recordSpan(trace,"subtask",task.role,"failed",(System.nanoTime()-started)/1_000_000d,error.toString(),Map.of("attempt",attempt,"status","retrying"));store.event("workflow_step_failed",Map.of("workflow",state.id,"role",task.role,"attempt",attempt,"error",error.toString()));}}
                if(last!=null){WorkflowState restored=store.restore(state.id,checkpoint);restored.status="paused";
                    for(WorkflowTask restoredTask:restored.tasks)if(restoredTask.id.equals(task.id)){restoredTask.result=last.toString();break;}
                    runtime.memory().updateProgress(state.id,task.role,"failed");store.saveWorkflow(restored);runtime.traces().finishTrace(trace,"paused",last.toString(),0.0);return restored;}state.updatedAt=JsonFiles.now();store.saveWorkflow(state);}
            state.status="completed";state.updatedAt=JsonFiles.now();store.saveWorkflow(state);runtime.traces().finishTrace(trace,"completed","",1.0);return state;}
    }

    public static String normalizeName(String value){return value.toLowerCase(Locale.ROOT).replaceAll("[^a-z0-9_-]+","_").replaceAll("^_+|_+$","");}
    private static double number(Object value,double fallback){return value instanceof Number n?n.doubleValue():fallback;}
}
