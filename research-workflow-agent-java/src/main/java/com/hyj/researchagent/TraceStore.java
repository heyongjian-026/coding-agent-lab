package com.hyj.researchagent;

import com.fasterxml.jackson.core.type.TypeReference;

import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/** 主/子 Agent 共用的文件 Trace、Token/费用预算和聚合指标。 */
public final class TraceStore {
    public record TraceBudget(Integer maxTokens,Double maxSeconds,Double maxCost) {
        public static TraceBudget fromEnvironment(){return new TraceBudget(integer("TRACE_MAX_TOKENS"),decimal("TRACE_MAX_SECONDS"),decimal("TRACE_MAX_COST"));}
    }
    @FunctionalInterface public interface ApprovalRequester{String request(Map<String,Object> payload);}
    @FunctionalInterface public interface ApprovalChecker{boolean approved(String id);}
    private final Path tracesPath,spansPath;private final TraceBudget budget;private final ApprovalRequester requester;private final ApprovalChecker checker;
    private final ThreadLocal<String> current=new ThreadLocal<>();private final double inputPrice,outputPrice;

    public TraceStore(Path dataDir){this(dataDir,TraceBudget.fromEnvironment(),null,null);}
    public TraceStore(Path dataDir,TraceBudget budget,ApprovalRequester requester,ApprovalChecker checker){
        tracesPath=dataDir.resolve("traces.json");spansPath=dataDir.resolve("trace_spans.jsonl");this.budget=budget;this.requester=requester;this.checker=checker;
        inputPrice=Double.parseDouble(setting("MODEL_INPUT_PRICE_PER_MILLION","0"));
        outputPrice=Double.parseDouble(setting("MODEL_OUTPUT_PRICE_PER_MILLION","0"));
    }

    /** 新建或恢复统一 trace_id，并记录单/多 Agent 模式和预算。 */
    public synchronized String startTrace(String taskType,String mode,String supplied){
        String id=supplied==null?JsonFiles.id("trace"):supplied;Map<String,Object> all=traces();Map<String,Object> trace;
        if(all.containsKey(id)){trace=JsonFiles.map(all.get(id));trace.put("duration_offset_ms",number(trace,"duration_ms"));trace.put("started_nanos",System.nanoTime());trace.put("status","running");trace.put("error","");}
        else {trace=new LinkedHashMap<>();trace.put("id",id);trace.put("task_type",taskType);trace.put("mode",mode);trace.put("status","running");
            trace.put("started_at",JsonFiles.now());trace.put("started_nanos",System.nanoTime());trace.put("input_tokens",0);trace.put("output_tokens",0);trace.put("cost",0.0);
            trace.put("duration_ms",0.0);trace.put("quality",null);trace.put("budget",JsonFiles.map(budget));trace.put("budget_approval_id",null);trace.put("budget_override",false);trace.put("error","");}
        all.put(id,trace);JsonFiles.write(tracesPath,all);current.set(id);return id;
    }
    public String startTrace(){return startTrace("research_task","single",null);}
    public void activate(String id){if(!traces().containsKey(id))throw new IllegalArgumentException("Unknown trace: "+id);current.set(id);}
    public String currentTraceId(){return current.get();}

    /** 记录模型 Token、费用和耗时，同时更新 trace 聚合值。 */
    public synchronized void recordModel(String id,String operation,double durationMs,int inputTokens,int outputTokens,String error){
        double cost=(inputTokens*inputPrice+outputTokens*outputPrice)/1_000_000d;
        recordSpan(id,"model",operation,error==null||error.isBlank()?"success":"failed",durationMs,inputTokens,outputTokens,cost,error,Map.of());
        Map<String,Object> all=traces();Map<String,Object> trace=JsonFiles.map(all.get(id));
        trace.put("input_tokens",integer(trace,"input_tokens")+inputTokens);trace.put("output_tokens",integer(trace,"output_tokens")+outputTokens);
        trace.put("cost",round(number(trace,"cost")+cost));all.put(id,trace);JsonFiles.write(tracesPath,all);
    }

    public void recordSpan(String id,String component,String operation,String status,double durationMs,String error,Map<String,Object> metadata){
        recordSpan(id,component,operation,status,durationMs,0,0,0,error,metadata);
    }
    public void recordSpan(String id,String component,String operation,String status,double durationMs,int in,int out,double cost,String error,Map<String,Object> metadata){
        Map<String,Object> span=new LinkedHashMap<>();span.put("trace_id",id);span.put("span_id",JsonFiles.id("span"));span.put("component",component);
        span.put("operation",operation);span.put("status",status);span.put("duration_ms",round(durationMs));span.put("input_tokens",in);span.put("output_tokens",out);
        span.put("cost",round(cost));span.put("error",error==null?"":error);span.put("metadata",metadata);span.put("time",JsonFiles.now());JsonFiles.appendJsonLine(spansPath,span);
    }

    /** 超预算时暂停并只创建一次人工审批；批准后同一 trace 获得 override。 */
    public synchronized Map<String,Object> checkBudget(String id){
        Map<String,Object> all=traces();Map<String,Object> trace=JsonFiles.map(all.get(id));if(Boolean.TRUE.equals(trace.get("budget_override")))return null;
        String approval=(String)trace.get("budget_approval_id");if(approval!=null&&checker!=null&&checker.approved(approval)){trace.put("budget_override",true);all.put(id,trace);JsonFiles.write(tracesPath,all);return null;}
        Map<String,Object> limit=JsonFiles.map(trace.get("budget"));List<String> exceeded=new ArrayList<>();
        int tokens=integer(trace,"input_tokens")+integer(trace,"output_tokens");double elapsed=(number(trace,"duration_offset_ms")+(System.nanoTime()-((Number)trace.get("started_nanos")).longValue())/1_000_000d)/1000d;
        if(limit.get("maxTokens")!=null&&tokens>=((Number)limit.get("maxTokens")).intValue())exceeded.add("tokens");
        if(limit.get("maxSeconds")!=null&&elapsed>=((Number)limit.get("maxSeconds")).doubleValue())exceeded.add("time");
        if(limit.get("maxCost")!=null&&number(trace,"cost")>=((Number)limit.get("maxCost")).doubleValue())exceeded.add("cost");
        if(exceeded.isEmpty())return null;if(approval==null&&requester!=null){approval=requester.request(Map.of("trace_id",id,"exceeded",exceeded));trace.put("budget_approval_id",approval);all.put(id,trace);JsonFiles.write(tracesPath,all);}
        Map<String,Object> result=new LinkedHashMap<>();result.put("trace_id",id);result.put("exceeded",exceeded);result.put("approval_id",approval);return result;
    }

    public synchronized void finishTrace(String id,String status,String error,Double quality){
        Map<String,Object> all=traces();Map<String,Object> trace=JsonFiles.map(all.get(id));trace.put("status",status);trace.put("error",error==null?"":error);trace.put("quality",quality);
        trace.put("duration_ms",round(number(trace,"duration_offset_ms")+(System.nanoTime()-((Number)trace.get("started_nanos")).longValue())/1_000_000d));trace.put("finished_at",JsonFiles.now());all.put(id,trace);JsonFiles.write(tracesPath,all);
    }

    public Map<String,Object> report(String id){Object trace=traces().get(id);if(trace==null)throw new IllegalArgumentException("Unknown trace: "+id);
        return Map.of("trace",trace,"spans",spans().stream().filter(s->id.equals(s.get("trace_id"))).toList());}

    /** 汇总任务/周报成功率、组件失败率、费用和延迟。 */
    public Map<String,Object> metrics(){
        List<Map<String,Object>> finished=traces().values().stream().map(JsonFiles::map).filter(t->!"running".equals(t.get("status"))).toList();
        List<Map<String,Object>> weekly=finished.stream().filter(t->"weekly_report".equals(t.get("task_type"))).toList();Map<String,int[]> components=new LinkedHashMap<>();
        for(Map<String,Object> span:spans()){int[] values=components.computeIfAbsent(String.valueOf(span.get("component")),k->new int[2]);values[0]++;if("failed".equals(span.get("status")))values[1]++;}
        Map<String,Double> failures=new LinkedHashMap<>();components.forEach((k,v)->failures.put(k,v[1]/(double)v[0]));Map<String,Object> result=new LinkedHashMap<>();
        result.put("task_success_rate",finished.isEmpty()?0:finished.stream().filter(t->"completed".equals(t.get("status"))).count()/(double)finished.size());
        result.put("weekly_report_success_rate",weekly.isEmpty()?0:weekly.stream().filter(t->"completed".equals(t.get("status"))).count()/(double)weekly.size());result.put("component_failure_rate",failures);
        result.put("total_cost",round(finished.stream().mapToDouble(t->number(t,"cost")).sum()));result.put("average_duration_ms",finished.isEmpty()?0:finished.stream().mapToDouble(t->number(t,"duration_ms")).average().orElse(0));return result;
    }

    /** 比较 single/multi 模式的平均质量、费用和延迟。 */
    public Map<String,Map<String,Double>> compareModes(){Map<String,List<Map<String,Object>>> groups=new LinkedHashMap<>();
        traces().values().stream().map(JsonFiles::map).filter(t->!"running".equals(t.get("status"))).forEach(t->groups.computeIfAbsent(String.valueOf(t.get("mode")),k->new ArrayList<>()).add(t));
        Map<String,Map<String,Double>> result=new LinkedHashMap<>();groups.forEach((mode,list)->{double quality=list.stream().filter(t->t.get("quality")!=null).mapToDouble(t->number(t,"quality")).average().orElse(0);
            result.put(mode,Map.of("quality",quality,"cost",list.stream().mapToDouble(t->number(t,"cost")).average().orElse(0),"latency_ms",list.stream().mapToDouble(t->number(t,"duration_ms")).average().orElse(0)));});return result;}

    private Map<String,Object> traces(){return JsonFiles.read(tracesPath,new TypeReference<>(){},new LinkedHashMap<>());}
    private List<Map<String,Object>> spans(){if(!Files.exists(spansPath))return List.of();try{return Files.readAllLines(spansPath).stream().filter(s->!s.isBlank()).map(s->{try{return JsonFiles.JSON.readValue(s,new TypeReference<Map<String,Object>>(){});}catch(Exception e){throw new IllegalStateException(e);}}).toList();}catch(Exception e){throw new IllegalStateException(e);}}
    private static int integer(Map<String,Object> map,String key){Object v=map.get(key);return v==null?0:((Number)v).intValue();}
    private static double number(Map<String,Object> map,String key){Object v=map.get(key);return v==null?0:((Number)v).doubleValue();}
    private static double round(double v){return Math.round(v*100_000_000d)/100_000_000d;}
    private static Integer integer(String name){String v=System.getenv(name);return v==null||v.isBlank()?null:Integer.valueOf(v);}
    private static Double decimal(String name){String v=System.getenv(name);return v==null||v.isBlank()?null:Double.valueOf(v);}
    private static String setting(String name,String fallback){String property=System.getProperty(name);return property!=null?property:System.getenv().getOrDefault(name,fallback);}
}
