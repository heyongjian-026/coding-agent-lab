package com.hyj.researchagent;

import com.fasterxml.jackson.core.type.TypeReference;

import java.nio.file.Path;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Set;
import java.util.function.Function;
import java.util.regex.Pattern;

/** 脱敏收集、人工审核、归因和重放科研 Bad Case。 */
public final class BadCaseStore {
    public record BadCase(String id,String caseType,String category,Object taskInput,List<Map<String,Object>> trajectory,
                          Object result,String failureStage,String source,String reviewStatus,String approvalId,
                          String rootCause,String fixedVersion,String retestResult,String createdAt,String updatedAt) {}
    @FunctionalInterface public interface ApprovalRequester { String request(Map<String,Object> payload); }
    @FunctionalInterface public interface ApprovalChecker { boolean approved(String id); }

    private static final Map<String,String> TYPES=Map.ofEntries(
            Map.entry("correction","output"),Map.entry("downvote","output"),Map.entry("rejection","output"),
            Map.entry("retrieval","retrieval"),Map.entry("citation","output"),Map.entry("weekly_report","output"),
            Map.entry("memory","memory"),Map.entry("agent_conflict","collaboration"),Map.entry("planning","planning"),
            Map.entry("tool","tool"),Map.entry("collaboration","collaboration"),Map.entry("output","output"));
    private static final Pattern BEARER=Pattern.compile("(?i)bearer\\s+[a-z0-9._-]+");
    private static final Pattern EMAIL=Pattern.compile("\\b[\\w.+-]+@[\\w.-]+\\.[A-Za-z]{2,}\\b");
    private static final Pattern PHONE=Pattern.compile("(?<!\\d)1[3-9]\\d{9}(?!\\d)");
    private static final Pattern KEY=Pattern.compile("\\b(?:sk|pk)-[A-Za-z0-9_-]{12,}\\b");
    private final Path candidates,library,replays; private final ApprovalRequester requester; private final ApprovalChecker checker;

    public BadCaseStore(Path dataDir,ApprovalRequester requester,ApprovalChecker checker){
        candidates=dataDir.resolve("bad_case_candidates.json");library=dataDir.resolve("bad_case_library.json");
        replays=dataDir.resolve("bad_case_replays.jsonl");this.requester=requester;this.checker=checker;
    }

    /** 收集候选案例；任何输入和轨迹都会先递归脱敏，候选不会直接进入正式库。 */
    public synchronized BadCase collect(String type,Object input,List<Map<String,Object>> trajectory,Object result,
                                        String failureStage,String source,String category){
        if(!TYPES.containsKey(type))throw new IllegalArgumentException("Unsupported Bad Case type '"+type+"'");
        String selected=category==null?TYPES.get(type):category;
        String id=JsonFiles.id("case");String approval=requester==null?null:requester.request(Map.of("case_id",id,"case_type",type,"category",selected));
        BadCase item=new BadCase(id,type,selected,redact(input),castTrajectory(redact(trajectory==null?List.of():trajectory)),redact(result),
                failureStage,source,"pending",approval,"","","",JsonFiles.now(),JsonFiles.now());
        Map<String,BadCase> all=read(candidates);all.put(id,item);JsonFiles.write(candidates,all);return item;
    }

    public BadCase collectFeedback(String type,Object input,Object result,String comment,List<Map<String,Object>> trajectory){
        if(!Set.of("correction","downvote","rejection").contains(type))throw new IllegalArgumentException("invalid feedback_type");
        return collect(type,input,trajectory,Map.of("result",result,"user_comment",comment),"user_feedback","user",null);
    }

    /** 只有关联审批已经通过，Reviewer 才能接受或拒绝候选案例。 */
    public synchronized BadCase review(String id,boolean approve,String reviewer){
        Map<String,BadCase> all=read(candidates);BadCase old=all.get(id);if(old==null)throw new IllegalArgumentException("Unknown case: "+id);
        if(old.approvalId()==null||checker==null||!checker.approved(old.approvalId()))throw new SecurityException("Human approval is required before Bad Case review");
        BadCase item=copy(old,approve?"accepted":"rejected",old.rootCause(),old.fixedVersion(),old.retestResult());
        all.put(id,item);JsonFiles.write(candidates,all);
        if(approve){Map<String,Object> accepted=new LinkedHashMap<>(JsonFiles.map(item));accepted.put("reviewer",reviewer);
            Map<String,Object> formal=JsonFiles.read(library,new TypeReference<>(){},new LinkedHashMap<>());formal.put(id,accepted);JsonFiles.write(library,formal);}
        return item;
    }

    public synchronized BadCase recordRemediation(String id,String rootCause,String fixedVersion,String retestResult){
        Map<String,BadCase> all=read(candidates);BadCase old=all.get(id);if(old==null)throw new IllegalArgumentException("Unknown case: "+id);
        BadCase item=copy(old,old.reviewStatus(),String.valueOf(redact(rootCause)),fixedVersion,String.valueOf(redact(retestResult)));
        all.put(id,item);JsonFiles.write(candidates,all);
        Map<String,Object> formal=JsonFiles.read(library,new TypeReference<>(){},new LinkedHashMap<>());
        if(formal.containsKey(id)){Map<String,Object> value=new LinkedHashMap<>((Map<String,Object>)formal.get(id));
            value.put("rootCause",item.rootCause());value.put("fixedVersion",fixedVersion);value.put("retestResult",item.retestResult());value.put("updatedAt",item.updatedAt());formal.put(id,value);JsonFiles.write(library,formal);}
        return item;
    }

    /** 按全文、类别和案例类型查询正式库或候选区。 */
    public List<Map<String,Object>> search(String query,String category,String type,boolean acceptedOnly){
        Map<String,Object> all=JsonFiles.read(acceptedOnly?library:candidates,new TypeReference<>(){},new LinkedHashMap<>());
        List<String> words=query==null?List.of():List.of(query.toLowerCase(Locale.ROOT).split("\\s+"));
        return all.values().stream().map(JsonFiles::map).filter(item->category==null||category.equals(field(item,"category")))
                .filter(item->type==null||type.equals(field(item,"caseType","case_type")))
                .filter(item->words.stream().allMatch(word->JsonFiles.toJson(item).toLowerCase(Locale.ROOT).contains(word)))
                .sorted((a,b)->field(b,"updatedAt","updated_at").compareTo(field(a,"updatedAt","updated_at"))).toList();
    }

    /** 只重放已审核案例，并将新结果追加到 JSONL。 */
    public synchronized Map<String,Object> replay(String id,Function<Map<String,Object>,Object> replayFunction){
        Map<String,Object> all=JsonFiles.read(library,new TypeReference<>(){},new LinkedHashMap<>());Object raw=all.get(id);
        if(raw==null)throw new IllegalArgumentException("Only accepted Bad Cases can be replayed");Map<String,Object> item=JsonFiles.map(raw);
        Map<String,Object> pack=Map.of("case_id",id,"task_input",value(item,"taskInput","task_input"),
                "trajectory",value(item,"trajectory"),"previous_result",value(item,"result"));
        Object result=replayFunction==null?pack:replayFunction.apply(pack);
        Map<String,Object> record=Map.of("time",JsonFiles.now(),"case_id",id,"result",redact(result));JsonFiles.appendJsonLine(replays,record);return record;
    }

    public static Object redact(Object value){
        if(value instanceof Map<?,?> map){Map<String,Object> result=new LinkedHashMap<>();map.forEach((k,v)->{
            String key=String.valueOf(k);result.put(key,key.matches("(?i).*?(api[_-]?key|token|password|secret).*?")?"[REDACTED]":redact(v));});return result;}
        if(value instanceof List<?> list)return list.stream().map(BadCaseStore::redact).toList();
        if(value==null||value instanceof Number||value instanceof Boolean)return value;
        String text=String.valueOf(value);text=BEARER.matcher(text).replaceAll("Bearer [REDACTED]");
        text=EMAIL.matcher(text).replaceAll("[REDACTED_EMAIL]");text=PHONE.matcher(text).replaceAll("[REDACTED_PHONE]");return KEY.matcher(text).replaceAll("[REDACTED_KEY]");
    }

    private BadCase copy(BadCase old,String status,String root,String version,String retest){return new BadCase(old.id(),old.caseType(),old.category(),old.taskInput(),old.trajectory(),old.result(),old.failureStage(),old.source(),status,old.approvalId(),root,version,retest,old.createdAt(),JsonFiles.now());}
    private Map<String,BadCase> read(Path path){return JsonFiles.read(path,new TypeReference<>(){},new LinkedHashMap<>());}
    @SuppressWarnings("unchecked") private static List<Map<String,Object>> castTrajectory(Object value){return (List<Map<String,Object>>)value;}
    private static Object value(Map<String,Object> map,String... names){for(String name:names)if(map.containsKey(name))return map.get(name);return null;}
    private static String field(Map<String,Object> map,String... names){return String.valueOf(value(map,names));}
}
