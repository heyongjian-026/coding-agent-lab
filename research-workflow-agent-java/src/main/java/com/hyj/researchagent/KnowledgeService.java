package com.hyj.researchagent;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.JsonNode;
import java.net.URI;
import java.net.http.*;
import java.nio.charset.StandardCharsets;
import java.nio.file.Path;
import java.security.MessageDigest;
import java.time.Duration;
import java.util.*;
import java.util.regex.*;

/** 文档切分、Embedding、混合检索、重排和引用校验。 */
public final class KnowledgeService {
    private static final int RRF_K=60;
    private static final double MIN_RELEVANCE=.05;
    private static final Pattern CITATION=Pattern.compile("\\[\\[([^]\\r\\n]+)]]");
    private static final Pattern WORD=Pattern.compile("[a-z0-9_]+|[\\p{IsHan}]");
    private static final Pattern HEADING=Pattern.compile("(?m)^#{1,6}\\s+(.+?)\\s*$");
    private KnowledgeService(){}

    public interface EmbeddingModel{
        List<List<Double>> embedMany(List<String> texts)throws Exception;
        default List<Double> embed(String text)throws Exception{return embedMany(List.of(text)).get(0);}
        int dimensions();
    }

    /** 无网络的确定性哈希 Embedding，仅用于开发、测试和降级演示。 */
    public static final class LocalEmbeddingModel implements EmbeddingModel{
        private final int dimensions;
        public LocalEmbeddingModel(){this(256);}
        public LocalEmbeddingModel(int dimensions){if(dimensions<=0)throw new IllegalArgumentException("dimensions must be positive");this.dimensions=dimensions;}
        public int dimensions(){return dimensions;}
        public List<List<Double>> embedMany(List<String> texts){return texts.stream().map(this::vector).toList();}
        private List<Double> vector(String text){double[] values=new double[dimensions];for(String token:terms(text))values[Math.floorMod(token.hashCode(),dimensions)]+=1;double norm=Math.sqrt(Arrays.stream(values).map(x->x*x).sum());List<Double> result=new ArrayList<>(dimensions);for(double value:values)result.add(norm==0?0:value/norm);return result;}
    }

    /** OpenAI-compatible Embedding HTTP 客户端，按 batchSize 分批请求。 */
    public static final class ApiEmbeddingModel implements EmbeddingModel{
        private final URI endpoint;private final String apiKey,model;private final int batchSize,dimensions;private final HttpClient http;
        public ApiEmbeddingModel(String url,String apiKey,String model,int batchSize,int dimensions){if(batchSize<=0||dimensions<=0)throw new IllegalArgumentException("batchSize and dimensions must be positive");endpoint=URI.create(url);this.apiKey=apiKey;this.model=model;this.batchSize=batchSize;this.dimensions=dimensions;http=HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(15)).build();}
        public int dimensions(){return dimensions;}
        public List<List<Double>> embedMany(List<String> texts)throws Exception{List<List<Double>> result=new ArrayList<>();for(int i=0;i<texts.size();i+=batchSize)result.addAll(request(texts.subList(i,Math.min(i+batchSize,texts.size()))));return result;}
        private List<List<Double>> request(List<String> texts)throws Exception{String body=JsonFiles.toJson(Map.of("model",model,"input",texts,"dimensions",dimensions));HttpRequest request=HttpRequest.newBuilder(endpoint).timeout(Duration.ofSeconds(60)).header("content-type","application/json").header("authorization","Bearer "+apiKey).POST(HttpRequest.BodyPublishers.ofString(body)).build();HttpResponse<String> response=http.send(request,HttpResponse.BodyHandlers.ofString());if(response.statusCode()/100!=2)throw new IllegalStateException("Embedding HTTP "+response.statusCode());List<Map.Entry<Integer,List<Double>>> indexed=new ArrayList<>();for(JsonNode item:JsonFiles.JSON.readTree(response.body()).path("data")){List<Double> vector=JsonFiles.JSON.convertValue(item.path("embedding"),new TypeReference<>(){});indexed.add(Map.entry(item.path("index").asInt(),vector));}return indexed.stream().sorted(Map.Entry.comparingByKey()).map(Map.Entry::getValue).toList();}
    }

    public interface KnowledgeBase{
        String addDocument(String title,String content,String source,int chunkSize)throws Exception;
        List<Map<String,Object>> search(String query,int limit)throws Exception;
    }
    public record DocumentChunk(String content,String section,int startOffset,int endOffset){}
    public record CitationCheck(boolean valid,List<String> citations,List<String> unknown,String message){}

    /** JSON 向量库：向量与 BM25 召回经 RRF 融合，再以词项覆盖率轻量重排。 */
    public static final class LocalKnowledgeBase implements KnowledgeBase{
        private final Path path;private final EmbeddingModel embedding;
        public LocalKnowledgeBase(Path dataDir){this(dataDir,new LocalEmbeddingModel());}
        public LocalKnowledgeBase(Path dataDir,EmbeddingModel embedding){path=dataDir.resolve("knowledge.json");this.embedding=embedding;}
        public synchronized String addDocument(String title,String content,String source,int chunkSize)throws Exception{String id=JsonFiles.id("doc");List<Map<String,Object>> all=read();List<DocumentChunk> chunks=chunkDocument(content,chunkSize,defaultOverlap(chunkSize));List<List<Double>> vectors=embedding.embedMany(chunks.stream().map(DocumentChunk::content).toList());for(int i=0;i<chunks.size();i++){DocumentChunk chunk=chunks.get(i);String chunkId=id+"-"+i;Map<String,Object> item=new LinkedHashMap<>();item.put("document_id",id);item.put("chunk_id",chunkId);item.put("citation_id",chunkId);item.put("title",title);item.put("source",source);item.put("section",chunk.section());item.put("chunk",i);item.put("start_offset",chunk.startOffset());item.put("end_offset",chunk.endOffset());item.put("content_hash",sha256(chunk.content()));item.put("content",chunk.content());item.put("embedding",vectors.get(i));all.add(item);}JsonFiles.write(path,all);return id;}
        public List<Map<String,Object>> search(String query,int limit)throws Exception{if(query==null||query.isBlank()||limit<=0)return List.of();List<Double> wanted=embedding.embed(query);List<Map<String,Object>> items=read();for(Map<String,Object> item:items){List<Double> vector=JsonFiles.JSON.convertValue(item.get("embedding"),new TypeReference<>(){});item.put("vector_score",cosine(wanted,vector));}return hybridRank(query,items,limit,embedding instanceof LocalEmbeddingModel);}
        private List<Map<String,Object>> read(){return JsonFiles.read(path,new TypeReference<>(){},new ArrayList<>());}
    }

    /** Elasticsearch 同时执行全文 match 与 kNN，并在应用层轻量重排。 */
    public static final class ElasticsearchKnowledgeBase implements KnowledgeBase{
        private final String baseUrl,index;private final EmbeddingModel embedding;private final HttpClient http;
        public ElasticsearchKnowledgeBase(String baseUrl,String index,EmbeddingModel embedding){this.baseUrl=baseUrl.replaceAll("/$","");this.index=index;this.embedding=embedding;http=HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(10)).build();}
        public String addDocument(String title,String content,String source,int chunkSize)throws Exception{String id=JsonFiles.id("doc");List<DocumentChunk> chunks=chunkDocument(content,chunkSize,defaultOverlap(chunkSize));List<List<Double>> vectors=embedding.embedMany(chunks.stream().map(DocumentChunk::content).toList());for(int i=0;i<chunks.size();i++){DocumentChunk chunk=chunks.get(i);String chunkId=id+"-"+i;Map<String,Object> item=new LinkedHashMap<>();item.put("document_id",id);item.put("chunk_id",chunkId);item.put("citation_id",chunkId);item.put("title",title);item.put("source",source);item.put("section",chunk.section());item.put("chunk",i);item.put("start_offset",chunk.startOffset());item.put("end_offset",chunk.endOffset());item.put("content_hash",sha256(chunk.content()));item.put("content",chunk.content());item.put("embedding",vectors.get(i));request("PUT","/"+index+"/_doc/"+chunkId,item);}return id;}
        public List<Map<String,Object>> search(String query,int limit)throws Exception{if(query==null||query.isBlank()||limit<=0)return List.of();int candidates=Math.max(limit*4,20);Map<String,Object> payload=new LinkedHashMap<>();payload.put("size",candidates);payload.put("query",Map.of("multi_match",Map.of("query",query,"fields",List.of("title^2","section^1.5","content"))));payload.put("knn",Map.of("field","embedding","query_vector",embedding.embed(query),"k",candidates,"num_candidates",Math.max(candidates*4,100)));JsonNode root=request("POST","/"+index+"/_search",payload);List<Map<String,Object>> result=new ArrayList<>();for(JsonNode hit:root.path("hits").path("hits")){Map<String,Object> item=JsonFiles.JSON.convertValue(hit.path("_source"),new TypeReference<>(){});item.put("vector_score",hit.path("_score").asDouble());result.add(item);}return hybridRank(query,result,limit,false);}
        private JsonNode request(String method,String path,Object payload)throws Exception{HttpRequest request=HttpRequest.newBuilder(URI.create(baseUrl+path)).timeout(Duration.ofSeconds(30)).header("content-type","application/json").method(method,HttpRequest.BodyPublishers.ofString(JsonFiles.toJson(payload))).build();HttpResponse<String> response=http.send(request,HttpResponse.BodyHandlers.ofString());if(response.statusCode()/100!=2)throw new IllegalStateException("Elasticsearch HTTP "+response.statusCode());return response.body().isBlank()?JsonFiles.JSON.createObjectNode():JsonFiles.JSON.readTree(response.body());}
    }

    /** 优先在段落和句末切块，长段落固定长度兜底，并保存重叠与定位元数据。 */
    public static List<DocumentChunk> chunkDocument(String content,int maxChars,int overlap){if(content==null||content.isBlank())return List.of();if(maxChars<32)throw new IllegalArgumentException("chunkSize must be at least 32");if(overlap<0||overlap>=maxChars)throw new IllegalArgumentException("overlap must be between 0 and chunkSize");List<DocumentChunk> result=new ArrayList<>();int start=0;while(start<content.length()){while(start<content.length()&&Character.isWhitespace(content.charAt(start)))start++;if(start>=content.length())break;int hardEnd=Math.min(start+maxChars,content.length()),end=hardEnd;if(hardEnd<content.length()){int floor=start+Math.max(16,maxChars/2);for(int i=hardEnd-1;i>=floor;i--){char c=content.charAt(i);if(c=='\n'||c=='。'||c=='！'||c=='？'||c=='.'||c=='!'||c=='?'||c==';'||c=='；'){end=i+1;break;}}}String text=content.substring(start,end).trim();if(!text.isBlank())result.add(new DocumentChunk(text,sectionAt(content,start),start,end));if(end>=content.length())break;start=Math.max(start+1,end-overlap);}return result;}

    /** 回答必须以 [[citation_id]] 引用本次检索真正返回的证据。 */
    public static CitationCheck validateCitations(String answer,Set<String> allowed){Set<String> found=new LinkedHashSet<>();Matcher matcher=CITATION.matcher(answer==null?"":answer);while(matcher.find())found.add(matcher.group(1).trim());List<String> unknown=found.stream().filter(id->!allowed.contains(id)).toList();boolean valid=allowed.isEmpty()||(!found.isEmpty()&&unknown.isEmpty());String message=valid?"citations valid":found.isEmpty()?"answer must cite retrieved evidence as [[citation_id]]":"unknown citations: "+unknown;return new CitationCheck(valid,List.copyOf(found),unknown,message);}

    static List<Map<String,Object>> hybridRank(String query,List<Map<String,Object>> items,int limit,boolean lexicalRequired){if(items.isEmpty())return List.of();List<String> queryTerms=terms(query);Map<Map<String,Object>,Double> keyword=bm25(queryTerms,items);List<Map<String,Object>> vectorRank=new ArrayList<>(items);vectorRank.sort(Comparator.comparingDouble(i->-number(i.get("vector_score"))));List<Map<String,Object>> keywordRank=new ArrayList<>(items);keywordRank.sort(Comparator.comparingDouble(i->-keyword.getOrDefault(i,0d)));Map<Map<String,Object>,Integer> vr=ranks(vectorRank),kr=ranks(keywordRank);double bestRrf=2d/(RRF_K+1);List<Map<String,Object>> scored=new ArrayList<>();for(Map<String,Object> original:items){double vector=Math.max(0,number(original.get("vector_score"))),lexical=keyword.getOrDefault(original,0d),coverage=coverage(queryTerms,String.valueOf(original.getOrDefault("content",""))),titleCoverage=coverage(queryTerms,String.valueOf(original.getOrDefault("title",""))+" "+original.getOrDefault("section",""));double rrf=1d/(RRF_K+vr.get(original))+(lexical>0?1d/(RRF_K+kr.get(original)):0),score=.60*(rrf/bestRrf)+.30*coverage+.10*titleCoverage;if((lexicalRequired&&coverage==0)||(!lexicalRequired&&Math.max(vector,coverage)<MIN_RELEVANCE))continue;Map<String,Object> copy=new LinkedHashMap<>(original);copy.remove("embedding");copy.put("keyword_score",lexical);copy.put("hybrid_score",rrf);copy.put("score",score);copy.putIfAbsent("citation_id",copy.getOrDefault("chunk_id",copy.get("document_id")+"-"+copy.getOrDefault("chunk",0)));scored.add(copy);}return scored.stream().sorted(Comparator.comparingDouble(i->-number(i.get("score")))).limit(limit).toList();}
    private static Map<Map<String,Object>,Double> bm25(List<String> query,List<Map<String,Object>> items){Map<Map<String,Object>,List<String>> docs=new HashMap<>();double avg=items.stream().mapToInt(i->{List<String> t=terms(String.valueOf(i.getOrDefault("title",""))+" "+i.getOrDefault("section","")+" "+i.getOrDefault("content",""));docs.put(i,t);return t.size();}).average().orElse(1);Map<Map<String,Object>,Double> result=new HashMap<>();for(Map<String,Object> item:items){List<String> doc=docs.get(item);Map<String,Long> tf=new HashMap<>();doc.forEach(t->tf.merge(t,1L,Long::sum));double score=0;for(String term:new LinkedHashSet<>(query)){long df=docs.values().stream().filter(d->d.contains(term)).count();double idf=Math.log(1+(items.size()-df+.5)/(df+.5));double f=tf.getOrDefault(term,0L);score+=idf*(f*2.2)/(f+1.2*(.25+.75*doc.size()/Math.max(1,avg)));}result.put(item,score);}return result;}
    private static Map<Map<String,Object>,Integer> ranks(List<Map<String,Object>> values){Map<Map<String,Object>,Integer> result=new HashMap<>();for(int i=0;i<values.size();i++)result.put(values.get(i),i+1);return result;}
    private static double coverage(List<String> query,String text){if(query.isEmpty())return 0;Set<String> doc=new LinkedHashSet<>(terms(text));long total=query.stream().distinct().count();return query.stream().distinct().filter(doc::contains).count()/(double)total;}
    static List<String> terms(String text){List<String> result=new ArrayList<>();Matcher matcher=WORD.matcher(text==null?"":text.toLowerCase(Locale.ROOT));while(matcher.find())result.add(matcher.group());return result;}
    public static double cosine(List<Double> left,List<Double> right){if(left.size()!=right.size())throw new IllegalArgumentException("embedding dimensions differ");double dot=0,a=0,b=0;for(int i=0;i<left.size();i++){dot+=left.get(i)*right.get(i);a+=left.get(i)*left.get(i);b+=right.get(i)*right.get(i);}return a==0||b==0?0:dot/(Math.sqrt(a)*Math.sqrt(b));}
    private static int defaultOverlap(int size){return Math.min(120,Math.max(0,size/6));}
    private static String sectionAt(String content,int offset){Matcher matcher=HEADING.matcher(content.substring(0,Math.min(offset,content.length())));String section="";while(matcher.find())section=matcher.group(1).trim();return section;}
    private static String sha256(String text){try{return HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(text.getBytes(StandardCharsets.UTF_8)));}catch(Exception error){throw new IllegalStateException(error);}}
    private static double number(Object value){return value instanceof Number n?n.doubleValue():0;}
}
