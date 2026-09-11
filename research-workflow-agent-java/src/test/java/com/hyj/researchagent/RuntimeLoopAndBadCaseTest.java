package com.hyj.researchagent;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.concurrent.atomic.AtomicInteger;

import static org.junit.jupiter.api.Assertions.*;

class RuntimeLoopAndBadCaseTest {
    @TempDir Path root;

    static final class FakeTransport implements McpSupport.Transport {
        final boolean fail;FakeTransport(boolean fail){this.fail=fail;}
        public Map<String,Object> request(String method,Map<String,Object> params){
            if(method.equals("tools/list"))return Map.of("tools",List.of(Map.of("name","search papers","description","search","annotations",Map.of("readOnlyHint",true),"inputSchema",Map.of("type","object"))));
            if(method.equals("tools/call")){if(fail)throw new IllegalStateException("MCP unavailable");return Map.of("content",List.of(Map.of("type","text","text","paper result")));}return Map.of();}
    }

    @Test void runtimePublishesCompleteToolPoolAndRestrictsSubagents() {
        ResearchRuntime runtime=new ResearchRuntime(root);
        assertEquals(38,runtime.assembleToolPool(null).size());
        assertEquals(List.of("search_knowledge"),runtime.assembleToolPool(java.util.Set.of("search_knowledge"))
                .stream().map(tool->String.valueOf(tool.get("name"))).toList());
    }

    @Test void mcpDiscoversDispatchesAndFallsBack() throws Exception {
        ResearchRuntime runtime=new ResearchRuntime(root);assertTrue(runtime.connectMcp("primary",new FakeTransport(true)).contains("Discovered"));assertTrue(runtime.connectMcp("backup",new FakeTransport(false)).contains("Discovered"));
        Files.writeString(runtime.dataDir().resolve("mcp_servers.json"),JsonFiles.toJson(Map.of("primary",Map.of("fallback_servers",List.of("backup")))));
        assertEquals("paper result",runtime.execute("mcp__primary__search_papers",Map.of("query","agents")));
        runtime.mcpClients().remove("backup");String first=runtime.execute("mcp__primary__search_papers",Map.of("query","offline"));String second=runtime.execute("mcp__primary__search_papers",Map.of("query","offline"));
        assertEquals(first,second);assertTrue(first.contains("pending task"));
    }

    @Test void mutatingMcpNeedsApproval() {
        McpSupport.Transport mutating=new McpSupport.Transport(){public Map<String,Object> request(String method,Map<String,Object> params){if(method.equals("tools/list"))return Map.of("tools",List.of(Map.of("name","write_note","inputSchema",Map.of("type","object"))));if(method.equals("tools/call"))return Map.of("content",List.of(Map.of("type","text","text","written")));return Map.of();}};
        ResearchRuntime runtime=new ResearchRuntime(root);runtime.connectMcp("zotero",mutating);String pending=runtime.execute("mcp__zotero__write_note",Map.of("text","draft"));String approval=pending.substring(pending.lastIndexOf(' ')+1);
        assertTrue(pending.contains("pending approval"));runtime.approvals().reviewApproval(approval,true);assertEquals("written",runtime.executeApprovedAction(approval));assertEquals("executed",runtime.approvals().getApproval(approval).status());
    }

    @Test void feedbackIsRedactedReviewedSearchedAndReplayed() {
        ResearchRuntime runtime=new ResearchRuntime(root);var item=runtime.badCases().collectFeedback("correction",Map.of("email","me@example.com","token","secret"),"Bearer abcdefghijklmnop","wrong",List.of(Map.of("phone","13812345678")));
        String json=JsonFiles.toJson(item);assertFalse(json.contains("me@example.com"));assertFalse(json.contains("13812345678"));assertThrows(SecurityException.class,()->runtime.badCases().review(item.id(),true,"lead"));
        runtime.approvals().reviewApproval(item.approvalId(),true);runtime.badCases().review(item.id(),true,"lead");runtime.badCases().recordRemediation(item.id(),"missing evidence","v2","passed");
        assertEquals(item.id(),runtime.badCases().search("missing evidence","output",null,true).get(0).get("id"));assertEquals(item.id(),runtime.badCases().replay(item.id(),null).get("case_id"));
    }

    @Test void runtimeFailureCreatesTraceAndCandidateCase() {
        ResearchRuntime runtime=new ResearchRuntime(root);String trace=runtime.traces().startTrace();String output=runtime.execute("unknown_tool",Map.of());runtime.traces().finishTrace(trace,"failed",output,0.0);
        assertEquals("tool",((List<Map<String,Object>>)runtime.traces().report(trace).get("spans")).get(0).get("component"));assertEquals("tool",runtime.badCases().search("",null,null,false).get(0).get("caseType"));
    }

    @Test void agentLoopRecallsMemoryPersistsAnswerAndTokens() throws Exception {
        ResearchRuntime runtime=new ResearchRuntime(root);runtime.memory().remember("用户偏好中文","preference");Map<String,String> captured=new java.util.HashMap<>();
        Resilience.ModelClient client=(model,messages,system,tools,max)->{captured.put("system",system);return new Resilience.ModelResponse(List.of(Map.of("type","text","text","回答")),5,2);};
        String answer=ResearchAgent.agentLoop(client,new ArrayList<>(List.of(Map.of("role","user","content","请按偏好回答"))),runtime,"model","s1","lead",null,"single","research_task",null,5);
        assertEquals("回答",answer);assertTrue(captured.get("system").contains("用户偏好中文"));assertEquals("回答",runtime.memory().loadSession("s1").messages().get(1).get("content"));
        Map<String,Object> trace=JsonFiles.map(runtime.traces().metrics());assertEquals(1.0,((Number)trace.get("task_success_rate")).doubleValue());
    }

    @Test void agentLoopExecutesToolAndReturnsResultToModel() throws Exception {
        ResearchRuntime runtime=new ResearchRuntime(root);AtomicInteger calls=new AtomicInteger();
        Resilience.ModelClient client=(model,messages,system,tools,max)->calls.getAndIncrement()==0
                ?new Resilience.ModelResponse(List.of(Map.of("type","tool_use","id","t1","name","remember","input",Map.of("content","stable fact"))),1,1)
                :new Resilience.ModelResponse(List.of(Map.of("type","text","text","done")),1,1);
        List<Map<String,Object>> history=new ArrayList<>(List.of(Map.of("role","user","content","remember this")));assertEquals("done",ResearchAgent.agentLoop(client,history,runtime,"model"));
        assertEquals("stable fact",runtime.memory().recall("stable").get(0).content());assertTrue(history.stream().anyMatch(m->m.get("content") instanceof List<?>));
    }
}
