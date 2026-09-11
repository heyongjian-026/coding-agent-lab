package com.hyj.researchagent;

import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;
import java.util.Map;
import java.util.concurrent.atomic.AtomicInteger;

import static org.junit.jupiter.api.Assertions.*;

class ResilienceAndObservabilityTest {
    @TempDir Path root;

    @AfterEach void clearPrices(){System.clearProperty("MODEL_INPUT_PRICE_PER_MILLION");System.clearProperty("MODEL_OUTPUT_PRICE_PER_MILLION");}

    @Test void modelRouterUsesComplexityAndFallsBack() throws Exception {
        AtomicInteger primary=new AtomicInteger(),fallback=new AtomicInteger(),light=new AtomicInteger();
        Resilience.ModelClient broken=(m,msg,s,t,max)->{primary.incrementAndGet();throw new IllegalStateException("unavailable");};
        Resilience.ModelClient backup=(m,msg,s,t,max)->{fallback.incrementAndGet();return new Resilience.ModelResponse(List.of(Map.of("type","text","text",m)),1,1);};
        Resilience.ModelClient cheap=(m,msg,s,t,max)->{light.incrementAndGet();return new Resilience.ModelResponse(List.of(Map.of("type","text","text",m)),1,1);};
        var router=new Resilience.ResilientModelClient(List.of(
                new Resilience.ModelEndpoint("primary",broken,"strong-1","strong",true),
                new Resilience.ModelEndpoint("fallback",backup,"strong-2","strong",false),
                new Resilience.ModelEndpoint("light",cheap,"light-1","light",false)),new Resilience.FallbackJournal(root));
        assertEquals("light-1",router.createForTask("light",List.of(),"",List.of(),100).content().get(0).get("text"));
        assertEquals("strong-2",router.createForTask("strong",List.of(),"",List.of(),100).content().get(0).get("text"));
        assertEquals("light",Resilience.classifyComplexity("简单问题"));assertEquals("strong",Resilience.classifyComplexity("请综合比较多篇论文架构"));
    }

    @Test void embeddingFallsBackToLocal() throws Exception {
        KnowledgeService.EmbeddingModel broken=new KnowledgeService.EmbeddingModel(){public List<List<Double>> embedMany(List<String> t){throw new IllegalStateException("offline");}public int dimensions(){return 2;}};
        KnowledgeService.EmbeddingModel local=new KnowledgeService.LocalEmbeddingModel(2);
        var value=new Resilience.FallbackEmbeddingModel(broken,local,new Resilience.FallbackJournal(root));
        assertEquals(2,value.embed("agent").size());assertTrue(Files.readString(root.resolve("fallback_events.jsonl")).contains("fallback"));
    }

    @Test void knowledgeFallbackDeduplicatesWrites() throws Exception {
        KnowledgeService.KnowledgeBase broken=new KnowledgeService.KnowledgeBase(){public String addDocument(String a,String b,String c,int d){throw new IllegalStateException("offline");}public List<Map<String,Object>> search(String q,int l){throw new IllegalStateException("offline");}};
        AtomicInteger writes=new AtomicInteger();KnowledgeService.KnowledgeBase local=new KnowledgeService.KnowledgeBase(){public String addDocument(String a,String b,String c,int d){writes.incrementAndGet();return "local";}public List<Map<String,Object>> search(String q,int l){return List.of(Map.of("content","hit"));}};
        var kb=new Resilience.FallbackKnowledgeBase(broken,local,new Resilience.FallbackJournal(root));
        assertEquals("local",kb.addDocument("t","c","manual",800));assertEquals("local",kb.addDocument("t","c","manual",800));assertEquals(1,writes.get());
        assertEquals("hit",kb.search("q",1).get(0).get("content"));
    }

    @Test void traceRecordsTokensCostBudgetAndOverride() {
        System.setProperty("MODEL_INPUT_PRICE_PER_MILLION","2");System.setProperty("MODEL_OUTPUT_PRICE_PER_MILLION","4");
        Map<String,Boolean> approvals=new java.util.HashMap<>();
        TraceStore store=new TraceStore(root,new TraceStore.TraceBudget(3,null,null),payload->{approvals.put("a1",false);return "a1";},id->approvals.get(id));
        String trace=store.startTrace();store.recordModel(trace,"lead",12,2,2,"");Map<String,Object> budget=store.checkBudget(trace);
        assertEquals(List.of("tokens"),budget.get("exceeded"));approvals.put("a1",true);assertNull(store.checkBudget(trace));store.finishTrace(trace,"completed","",0.9);
        Map<String,Object> saved=JsonFiles.map(store.report(trace).get("trace"));assertEquals(2,((Number)saved.get("input_tokens")).intValue());assertEquals(0.000012,((Number)saved.get("cost")).doubleValue(),0.0000001);
    }

    @Test void metricsAndModeComparisonWork() {
        TraceStore store=new TraceStore(root);String single=store.startTrace("weekly_report","single",null);store.recordSpan(single,"rag","search","failed",3,"offline",Map.of());store.finishTrace(single,"failed","offline",0.2);
        String multi=store.startTrace("research_task","multi",null);store.recordSpan(multi,"subtask","researcher","success",2,"",Map.of("attempt",2));store.finishTrace(multi,"completed","",0.8);
        assertEquals(0.5,((Number)store.metrics().get("task_success_rate")).doubleValue());assertEquals(1.0,((Map<?,?>)store.metrics().get("component_failure_rate")).get("rag"));
        assertEquals(0.2,store.compareModes().get("single").get("quality"));assertEquals(0.8,store.compareModes().get("multi").get("quality"));
    }
}
