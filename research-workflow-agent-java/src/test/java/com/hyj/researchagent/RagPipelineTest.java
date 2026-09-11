package com.hyj.researchagent;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;
import java.nio.file.Path;
import java.util.*;
import java.util.concurrent.atomic.AtomicInteger;
import static org.junit.jupiter.api.Assertions.*;

class RagPipelineTest {
    @TempDir Path root;

    @Test void structuredChunksKeepOverlapSectionAndOffsets(){
        String text="# Methods\n"+"alpha beta gamma. ".repeat(20)+"\n\n# Results\n"+"delta epsilon. ".repeat(20);
        var chunks=KnowledgeService.chunkDocument(text,120,20);
        assertTrue(chunks.size()>2);assertTrue(chunks.stream().anyMatch(c->c.section().equals("Methods")));
        assertTrue(chunks.get(1).startOffset()<chunks.get(0).endOffset());assertTrue(chunks.stream().allMatch(c->c.endOffset()>c.startOffset()));
    }

    @Test void hybridSearchReranksExactTermsAndReturnsCitationMetadata()throws Exception{
        var kb=new KnowledgeService.LocalKnowledgeBase(root,new KnowledgeService.LocalEmbeddingModel(64));
        kb.addDocument("Dense retrieval","semantic vectors find related passages","dense",128);
        kb.addDocument("BM25","BM25 uses inverse document frequency for exact terminology","lexical",128);
        var hit=kb.search("BM25 inverse document frequency",2).get(0);
        assertEquals("lexical",hit.get("source"));assertNotNull(hit.get("keyword_score"));assertNotNull(hit.get("citation_id"));assertNotNull(hit.get("start_offset"));
    }

    @Test void irrelevantQueryIsRejected()throws Exception{
        var kb=new KnowledgeService.LocalKnowledgeBase(root,new KnowledgeService.LocalEmbeddingModel(4096));
        kb.addDocument("RAG","retrieval augmented generation uses documents","rag",128);
        assertTrue(kb.search("zxqv banana telescope",3).isEmpty());
    }

    @Test void citationValidatorRejectsMissingAndInventedIds(){
        Set<String> allowed=Set.of("doc-1");
        assertFalse(KnowledgeService.validateCitations("answer",allowed).valid());
        assertFalse(KnowledgeService.validateCitations("answer [[fake]]",allowed).valid());
        assertTrue(KnowledgeService.validateCitations("answer [[doc-1]]",allowed).valid());
    }

    @Test void agentRequestsCitationRepairAfterKnowledgeSearch()throws Exception{
        ResearchRuntime runtime=new ResearchRuntime(root);runtime.execute("add_document",Map.of("title","RAG","content","RAG retrieves evidence documents for generation.","source","paper"));AtomicInteger calls=new AtomicInteger();String[] citation={null};
        Resilience.ModelClient client=(model,messages,system,tools,max)->switch(calls.getAndIncrement()){
            case 0->new Resilience.ModelResponse(List.of(Map.of("type","tool_use","id","t1","name","search_knowledge","input",Map.of("query","RAG evidence"))),1,1);
            case 1->new Resilience.ModelResponse(List.of(Map.of("type","text","text","RAG uses evidence.")),1,1);
            default->{for(Map<String,Object> message:messages)if(message.get("content") instanceof List<?> blocks)for(Object raw:blocks){Map<String,Object> block=JsonFiles.map(raw);if("tool_result".equals(block.get("type"))){List<Map<String,Object>> hits=JsonFiles.JSON.readValue(String.valueOf(block.get("content")),new com.fasterxml.jackson.core.type.TypeReference<>(){});citation[0]=String.valueOf(hits.get(0).get("citation_id"));}}yield new Resilience.ModelResponse(List.of(Map.of("type","text","text","RAG uses evidence [["+citation[0]+"]]")),1,1);}
        };
        String answer=ResearchAgent.agentLoop(client,new ArrayList<>(List.of(Map.of("role","user","content","Explain RAG evidence"))),runtime,"model");
        assertEquals(3,calls.get());assertTrue(answer.contains("[["));
    }

    @Test void twentyQuestionRagSuiteMeetsBaseline()throws Exception{
        Map<String,Object> report=RagEvalRunner.run(Path.of("evals/rag-tasks.json"),root.resolve("eval"));Map<?,?> metrics=(Map<?,?>)report.get("metrics");
        assertEquals(21,((Number)metrics.get("case_count")).intValue());assertTrue(((Number)metrics.get("recall_at_3")).doubleValue()>=.9);assertEquals(1d,((Number)metrics.get("citation_metadata_rate")).doubleValue());assertEquals(1d,((Number)metrics.get("irrelevant_query_rejection_rate")).doubleValue());
    }
}
