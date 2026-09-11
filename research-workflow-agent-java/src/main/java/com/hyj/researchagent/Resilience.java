package com.hyj.researchagent;

import com.fasterxml.jackson.core.type.TypeReference;

import java.nio.charset.StandardCharsets;
import java.nio.file.Path;
import java.security.MessageDigest;
import java.util.ArrayList;
import java.util.HexFormat;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/** 模型、Embedding、知识库和外部数据源的幂等降级组件。 */
public final class Resilience {
    private Resilience() {}

    @FunctionalInterface public interface Operation<T> { T run() throws Exception; }
    public record Provider<T>(String name, Operation<T> operation) {}

    public static String operationId(String service, Object payload) {
        try {
            String canonical = JsonFiles.JSON.writer().with(com.fasterxml.jackson.databind.SerializationFeature.ORDER_MAP_ENTRIES_BY_KEYS)
                    .writeValueAsString(payload);
            String hash = HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(canonical.getBytes(StandardCharsets.UTF_8)));
            return service + "_" + hash.substring(0,20);
        } catch (Exception error) { throw new IllegalStateException(error); }
    }

    /** 按任务语义和长度确定轻量或强模型，不把路由决策交给模型自身。 */
    public static String classifyComplexity(String text) {
        String lower = text.toLowerCase();
        return text.length()>180 || List.of("比较","综合","评审","架构","多篇","experiment","compare","synthesize","review","architecture")
                .stream().anyMatch(lower::contains) ? "strong" : "light";
    }

    /** 追加切换事件、缓存幂等结果并保存无法完成的外部任务。 */
    public static final class FallbackJournal {
        private final Path events, cache, pending;
        public FallbackJournal(Path dataDir) {
            events=dataDir.resolve("fallback_events.jsonl"); cache=dataDir.resolve("fallback_results.json");
            pending=dataDir.resolve("pending_external_tasks.json");
        }
        public synchronized <T> T execute(String service,String operationId,List<Provider<T>> providers,boolean cacheResult) throws Exception {
            Map<String,Object> stored=JsonFiles.read(cache,new TypeReference<>(){},new LinkedHashMap<>());
            if(stored.containsKey(operationId)) {
                log(operationId,service,"cache","cache","reused","idempotent replay");
                @SuppressWarnings("unchecked") T result=(T)stored.get(operationId); return result;
            }
            List<String> errors=new ArrayList<>();
            for(int i=0;i<providers.size();i++) {
                Provider<T> provider=providers.get(i);
                try {
                    T result=provider.operation().run();
                    log(operationId,service,i==0?provider.name():providers.get(i-1).name(),provider.name(),"succeeded",i==0?"primary":"fallback");
                    if(cacheResult){stored.put(operationId,result);JsonFiles.write(cache,stored);} return result;
                } catch(Exception error) {
                    errors.add(provider.name()+": "+error.getMessage());
                    log(operationId,service,provider.name(),i+1<providers.size()?providers.get(i+1).name():"pending","failed",error.toString());
                }
            }
            throw new IllegalStateException(String.join("; ",errors));
        }
        public synchronized String defer(String service,Map<String,Object> payload,String reason) {
            String id=operationId(service,payload); Map<String,Object> all=JsonFiles.read(pending,new TypeReference<>(){},new LinkedHashMap<>());
            all.putIfAbsent(id,Map.of("id",id,"service",service,"payload",payload,"reason",reason,"status","pending","created_at",JsonFiles.now()));
            JsonFiles.write(pending,all); log(id,service,"all","pending","deferred",reason); return id;
        }
        public void log(String id,String service,String from,String to,String status,String reason) {
            JsonFiles.appendJsonLine(events,Map.of("time",JsonFiles.now(),"operation_id",id,"service",service,
                    "from",from,"to",to,"status",status,"reason",reason));
        }
    }

    @FunctionalInterface public interface ModelClient { ModelResponse create(String model,List<Map<String,Object>> messages,String system,List<Map<String,Object>> tools,int maxTokens) throws Exception; }
    public record ModelResponse(List<Map<String,Object>> content,int inputTokens,int outputTokens) {}
    public record ModelEndpoint(String name,ModelClient client,String model,String tier,boolean primary) {}

    /** 按复杂度排序主备模型，并通过统一接口自动切换。 */
    public static final class ResilientModelClient implements ModelClient {
        private final List<ModelEndpoint> endpoints; private final FallbackJournal journal;
        public ResilientModelClient(List<ModelEndpoint> endpoints,FallbackJournal journal) {
            if(endpoints.isEmpty()) throw new IllegalArgumentException("At least one model endpoint is required");
            this.endpoints=List.copyOf(endpoints);this.journal=journal;
        }
        public ModelResponse createForTask(String complexity,List<Map<String,Object>> messages,String system,List<Map<String,Object>> tools,int maxTokens) throws Exception {
            List<ModelEndpoint> ordered=endpoints.stream().sorted(java.util.Comparator
                    .comparing((ModelEndpoint e)->!e.tier().equals(complexity)).thenComparing(e->!e.primary())).toList();
            String id=operationId("model",Map.of("complexity",complexity,"messages",messages));
            List<Provider<ModelResponse>> providers=ordered.stream().map(endpoint->new Provider<ModelResponse>(endpoint.name(),
                    ()->endpoint.client().create(endpoint.model(),messages,system,tools,maxTokens))).toList();
            return journal.execute("model",id,providers,false);
        }
        public ModelResponse create(String model,List<Map<String,Object>> messages,String system,List<Map<String,Object>> tools,int maxTokens) throws Exception {
            return createForTask("strong",messages,system,tools,maxTokens);
        }
    }

    /** 远程 Embedding 失败时切换本地实现。 */
    public static final class FallbackEmbeddingModel implements KnowledgeService.EmbeddingModel {
        private final KnowledgeService.EmbeddingModel primary,fallback; private final FallbackJournal journal;
        public FallbackEmbeddingModel(KnowledgeService.EmbeddingModel primary,KnowledgeService.EmbeddingModel fallback,FallbackJournal journal){
            this.primary=primary;this.fallback=fallback;this.journal=journal;
        }
        public int dimensions(){return primary.dimensions()>0?primary.dimensions():fallback.dimensions();}
        public List<List<Double>> embedMany(List<String> texts)throws Exception{
            return journal.execute("embedding",operationId("embedding",texts),List.of(
                    new Provider<>("remote",()->primary.embedMany(texts)),new Provider<>("local",()->fallback.embedMany(texts))),false);
        }
    }

    /** Elasticsearch 失败时使用本地知识库，写入结果按 operationId 去重。 */
    public static final class FallbackKnowledgeBase implements KnowledgeService.KnowledgeBase {
        private final KnowledgeService.KnowledgeBase primary,fallback; private final FallbackJournal journal;
        public FallbackKnowledgeBase(KnowledgeService.KnowledgeBase primary,KnowledgeService.KnowledgeBase fallback,FallbackJournal journal){
            this.primary=primary;this.fallback=fallback;this.journal=journal;
        }
        public String addDocument(String title,String content,String source,int chunkSize)throws Exception{
            Map<String,Object> payload=Map.of("title",title,"content",content,"source",source,"chunk_size",chunkSize);
            String id=operationId("knowledge_write",payload);
            return journal.execute("knowledge_write",id,List.of(
                    new Provider<>("elasticsearch",()->{String result=primary.addDocument(title,content,source,chunkSize);fallback.addDocument(title,content,source,chunkSize);return result;}),
                    new Provider<>("local",()->fallback.addDocument(title,content,source,chunkSize))),true);
        }
        public List<Map<String,Object>> search(String query,int limit)throws Exception{
            return journal.execute("knowledge_search",operationId("knowledge_search",Map.of("query",query,"limit",limit)),List.of(
                    new Provider<>("elasticsearch",()->primary.search(query,limit)),new Provider<>("local",()->fallback.search(query,limit))),false);
        }
    }
}
