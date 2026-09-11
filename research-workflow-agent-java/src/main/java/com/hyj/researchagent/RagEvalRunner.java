package com.hyj.researchagent;

import com.fasterxml.jackson.core.type.TypeReference;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/** 确定性 RAG 检索评测：计算 Recall@K、MRR、引用元数据完整率和无关问题拒答率。 */
public final class RagEvalRunner {
    private RagEvalRunner(){}

    public static Map<String,Object> run(Path dataset,Path workspace)throws Exception{
        Map<String,Object> suite=JsonFiles.JSON.readValue(dataset.toFile(),new TypeReference<>(){});
        KnowledgeService.LocalKnowledgeBase kb=new KnowledgeService.LocalKnowledgeBase(workspace,new KnowledgeService.LocalEmbeddingModel(256));
        for(Object raw:(List<?>)suite.get("documents")){Map<String,Object> doc=JsonFiles.map(raw);kb.addDocument(String.valueOf(doc.get("title")),String.valueOf(doc.get("content")),String.valueOf(doc.get("source")),256);}
        List<Map<String,Object>> cases=new ArrayList<>();int hit=0,rejected=0,citationComplete=0;double reciprocal=0;
        for(Object raw:(List<?>)suite.get("cases")){Map<String,Object> item=JsonFiles.map(raw);String query=String.valueOf(item.get("query"));String expected=String.valueOf(item.getOrDefault("expected_source",""));boolean expectNoResult=Boolean.TRUE.equals(item.get("expect_no_result"));List<Map<String,Object>> results=kb.search(query,3);int rank=0;for(int i=0;i<results.size();i++)if(expected.equals(String.valueOf(results.get(i).get("source")))){rank=i+1;break;}boolean passed=expectNoResult?results.isEmpty():rank>0;if(expectNoResult&&passed)rejected++;if(!expectNoResult&&rank>0){hit++;reciprocal+=1d/rank;}if(results.stream().allMatch(RagEvalRunner::hasCitationMetadata))citationComplete++;
            cases.add(Map.of("id",item.get("id"),"passed",passed,"rank",rank,"result_count",results.size()));}
        int total=cases.size(),answerable=(int)((List<?>)suite.get("cases")).stream().map(JsonFiles::map).filter(c->!Boolean.TRUE.equals(c.get("expect_no_result"))).count(),unanswerable=total-answerable;Map<String,Object> metrics=new LinkedHashMap<>();metrics.put("case_count",total);metrics.put("recall_at_3",answerable==0?1d:hit/(double)answerable);metrics.put("mrr",answerable==0?1d:reciprocal/answerable);metrics.put("citation_metadata_rate",citationComplete/(double)Math.max(1,total));metrics.put("irrelevant_query_rejection_rate",unanswerable==0?1d:rejected/(double)unanswerable);
        return Map.of("suite",suite.get("suite"),"metrics",metrics,"cases",cases);
    }

    private static boolean hasCitationMetadata(Map<String,Object> item){return item.get("citation_id")!=null&&item.get("source")!=null&&item.get("start_offset")!=null&&item.get("end_offset")!=null;}

    public static void main(String[] args)throws Exception{Path dataset=Path.of(args.length>0?args[0]:"evals/rag-tasks.json");Path temp=Files.createTempDirectory("research-rag-eval-");Map<String,Object> report=run(dataset,temp);System.out.println(JsonFiles.toJson(report));double recall=((Number)((Map<?,?>)report.get("metrics")).get("recall_at_3")).doubleValue();if(recall<.9)System.exit(1);}
}
