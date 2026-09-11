package com.hyj.researchagent;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;

/** 具备三级记忆召回、工具调用、Token Trace、预算暂停和重试的基础 Agent Loop。 */
public final class ResearchAgent {
    private ResearchAgent() {}

    public static String agentLoop(Resilience.ModelClient client,List<Map<String,Object>> messages,
                                   ResearchRuntime runtime,String model,String sessionId,String agentName,
                                   String traceId,String mode,String taskType,Set<String> allowedTools,int maxRounds)throws Exception{
        String session=sessionId==null?"default":sessionId;String request=latestUser(messages);
        runtime.memory().appendTurn(session,"user",request);
        String trace=runtime.traces().startTrace(taskType==null?"research_task":taskType,mode==null?"single":mode,traceId);
        List<Map<String,Object>> active=new ArrayList<>(runtime.memory().compressMessages(messages,session,12,6));
        String complexity=Resilience.classifyComplexity(request);List<Map<String,Object>> tools=runtime.assembleToolPool(allowedTools);
        Set<String> retrievedCitations=new java.util.LinkedHashSet<>();
        for(int round=0;round<maxRounds;round++){
            Map<String,Object> budget=runtime.traces().checkBudget(trace);
            if(budget!=null){runtime.traces().finishTrace(trace,"paused","budget exceeded",null);return "Paused: budget exceeded "+budget.get("exceeded")+"; approval="+budget.get("approval_id");}
            long started=System.nanoTime();Resilience.ModelResponse response;
            try{
                response=retry(()->client instanceof Resilience.ResilientModelClient router
                        ?router.createForTask(complexity,active,systemPrompt(runtime,request,session,agentName),tools,8000)
                        :client.create(model,active,systemPrompt(runtime,request,session,agentName),tools,8000),3);
                runtime.traces().recordModel(trace,agentName==null?"lead":agentName,(System.nanoTime()-started)/1_000_000d,response.inputTokens(),response.outputTokens(),"");
            }catch(Exception error){runtime.traces().recordModel(trace,agentName==null?"lead":agentName,(System.nanoTime()-started)/1_000_000d,0,0,error.toString());runtime.traces().finishTrace(trace,"failed",error.toString(),0.0);throw error;}
            active.add(Map.of("role","assistant","content",response.content()));List<Map<String,Object>> calls=response.content().stream().filter(block->"tool_use".equals(block.get("type"))).toList();
            if(calls.isEmpty()){String answer=response.content().stream().filter(block->"text".equals(block.get("type"))).map(block->String.valueOf(block.getOrDefault("text",""))).reduce((a,b)->a+"\n"+b).orElse("");
                KnowledgeService.CitationCheck citationCheck=KnowledgeService.validateCitations(answer,retrievedCitations);
                if(!citationCheck.valid()&&round+1<maxRounds){active.add(Map.of("role","user","content","Citation validation failed: "+citationCheck.message()+". Rewrite the answer using only the retrieved citation_id values."));continue;}
                if(!citationCheck.valid()){runtime.traces().finishTrace(trace,"failed",citationCheck.message(),0.0);return "Error: "+citationCheck.message();}
                runtime.memory().appendTurn(session,"assistant",answer);runtime.traces().finishTrace(trace,"completed","",1.0);messages.clear();messages.addAll(active);return answer;}
            List<Map<String,Object>> results=new ArrayList<>();for(Map<String,Object> call:calls){String name=String.valueOf(call.get("name"));Map<String,Object> args=call.get("input") instanceof Map<?,?>?JsonFiles.map(call.get("input")):Map.of();String output=runtime.execute(name,args);
                if("search_knowledge".equals(name))collectCitationIds(output,retrievedCitations);
                Map<String,Object> result=new LinkedHashMap<>();result.put("type","tool_result");result.put("tool_use_id",String.valueOf(call.get("id")));result.put("content",output);results.add(result);}
            active.add(Map.of("role","user","content",results));
        }
        runtime.traces().finishTrace(trace,"round_limit","maximum rounds reached",0.0);return "Stopped: maximum Agent rounds reached";
    }

    public static String agentLoop(Resilience.ModelClient client,List<Map<String,Object>> messages,ResearchRuntime runtime,String model)throws Exception{
        return agentLoop(client,messages,runtime,model,"default","lead",null,"single","research_task",null,30);
    }

    /** 系统提示包含角色身份、工作区、长期召回和短期摘要。 */
    public static String systemPrompt(ResearchRuntime runtime,String query,String session,String agentName){return "You are "+(agentName==null?"lead":agentName)+" in a research workflow. Workspace: "+runtime.workspace()+
            ". Use tools for evidence. Every claim based on search_knowledge must cite its returned citation_id as [[citation_id]]. Never invent citations. Respect approval gates, and never write unreviewed results to long-term memory. Memory context: "+runtime.memory().buildContext(query,session,5);}

    @FunctionalInterface private interface Operation<T>{T run()throws Exception;}
    static <T>T retry(Operation<T> operation,int attempts)throws Exception{Exception last=null;for(int i=0;i<attempts;i++)try{return operation.run();}catch(Exception error){String text=error.toString().toLowerCase();if(!(text.contains("429")||text.contains("529")||text.contains("overload")||text.contains("temporar")))throw error;last=error;if(i+1<attempts)Thread.sleep((1L<<i)*10);}throw new IllegalStateException("Transient API error after retries",last);}
    private static void collectCitationIds(String output,Set<String> target){try{for(Map<String,Object> item:JsonFiles.JSON.readValue(output,new com.fasterxml.jackson.core.type.TypeReference<List<Map<String,Object>>>(){})){Object id=item.get("citation_id");if(id!=null)target.add(String.valueOf(id));}}catch(Exception ignored){/* 工具错误不是可引用证据。 */}}
    private static String latestUser(List<Map<String,Object>> messages){for(int i=messages.size()-1;i>=0;i--)if("user".equals(messages.get(i).get("role"))&&messages.get(i).get("content") instanceof String text)return text;return "";}
}
