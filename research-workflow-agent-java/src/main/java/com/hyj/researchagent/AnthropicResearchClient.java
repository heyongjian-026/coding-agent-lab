package com.hyj.researchagent;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.node.ObjectNode;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;

/** Anthropic Messages API 的 Java HttpClient 适配器。 */
public final class AnthropicResearchClient implements Resilience.ModelClient {
    private final String key;private final URI endpoint;private final HttpClient http=HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(20)).build();
    public AnthropicResearchClient(String apiKey,String baseUrl){if(apiKey==null||apiKey.isBlank())throw new IllegalArgumentException("ANTHROPIC_API_KEY is required");key=apiKey;endpoint=URI.create((baseUrl==null||baseUrl.isBlank()?"https://api.anthropic.com":baseUrl.replaceAll("/$",""))+"/v1/messages");}
    public Resilience.ModelResponse create(String model,List<Map<String,Object>> messages,String system,List<Map<String,Object>> tools,int maxTokens)throws Exception{
        ObjectNode body=JsonFiles.JSON.createObjectNode();body.put("model",model);body.put("system",system);body.put("max_tokens",maxTokens);body.set("messages",JsonFiles.JSON.valueToTree(messages));body.set("tools",JsonFiles.JSON.valueToTree(tools));
        HttpRequest request=HttpRequest.newBuilder(endpoint).timeout(Duration.ofMinutes(3)).header("content-type","application/json").header("x-api-key",key).header("anthropic-version","2023-06-01").POST(HttpRequest.BodyPublishers.ofString(JsonFiles.toJson(body))).build();
        HttpResponse<String> response=http.send(request,HttpResponse.BodyHandlers.ofString());if(response.statusCode()/100!=2)throw new IllegalStateException("Anthropic HTTP "+response.statusCode()+": "+response.body());JsonNode root=JsonFiles.JSON.readTree(response.body());
        List<Map<String,Object>> content=new ArrayList<>();for(JsonNode block:root.path("content"))content.add(JsonFiles.JSON.convertValue(block,new TypeReference<>(){}));JsonNode usage=root.path("usage");return new Resilience.ModelResponse(content,usage.path("input_tokens").asInt(),usage.path("output_tokens").asInt());}
}
