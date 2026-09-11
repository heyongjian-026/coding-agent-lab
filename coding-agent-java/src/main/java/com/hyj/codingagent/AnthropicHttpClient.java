package com.hyj.codingagent;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.ObjectNode;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;

/**
 * Anthropic Messages API 的轻量 Java HTTP 适配器。
 *
 * <p>AgentLoop 只依赖 ModelClient，本类负责把领域消息、工具 Schema 和响应块
 * 转换为 Anthropic JSON，因此未来可以新增其他模型实现而无需修改主循环。</p>
 */
public final class AnthropicHttpClient implements AgentLoop.ModelClient {
    private static final ObjectMapper JSON = new ObjectMapper();
    private final HttpClient http = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(20)).build();
    private final String apiKey;
    private final URI endpoint;

    /** 使用显式 API Key 和可选兼容端点创建客户端。 */
    public AnthropicHttpClient(String apiKey, String baseUrl) {
        if (apiKey == null || apiKey.isBlank()) throw new IllegalArgumentException("ANTHROPIC_API_KEY is required");
        this.apiKey = apiKey;
        String root = baseUrl == null || baseUrl.isBlank() ? "https://api.anthropic.com" : baseUrl.replaceAll("/$", "");
        this.endpoint = URI.create(root + "/v1/messages");
    }

    @Override
    /** 发送一次模型请求，并把 text/tool_use 内容解析为内部 Block。 */
    public AgentLoop.ModelResponse create(AgentLoop.ModelRequest request) throws Exception {
        ObjectNode body = JSON.createObjectNode();
        body.put("model", request.model()); body.put("system", request.system()); body.put("max_tokens", request.maxTokens());
        ArrayNode messages = body.putArray("messages");
        for (AgentLoop.Message message : request.messages()) {
            ObjectNode item = messages.addObject(); item.put("role", message.role());
            item.set("content", contentNode(message.content()));
        }
        body.set("tools", toolDefinitions());
        HttpRequest httpRequest = HttpRequest.newBuilder(endpoint).timeout(Duration.ofMinutes(3))
                .header("content-type", "application/json").header("x-api-key", apiKey)
                .header("anthropic-version", "2023-06-01")
                .POST(HttpRequest.BodyPublishers.ofString(JSON.writeValueAsString(body))).build();
        HttpResponse<String> response = http.send(httpRequest, HttpResponse.BodyHandlers.ofString());
        // 非 2xx 作为异常交给 AgentLoop 判断是否属于 429/529 等可重试错误。
        if (response.statusCode() / 100 != 2) throw new IllegalStateException("Anthropic HTTP " + response.statusCode() + ": " + response.body());
        JsonNode root = JSON.readTree(response.body());
        List<AgentLoop.Block> blocks = new ArrayList<>();
        for (JsonNode block : root.path("content")) {
            if (block.path("type").asText().equals("text")) blocks.add(new AgentLoop.TextBlock(block.path("text").asText()));
            else if (block.path("type").asText().equals("tool_use")) {
                @SuppressWarnings("unchecked") Map<String, Object> input = JSON.convertValue(block.path("input"), Map.class);
                blocks.add(new AgentLoop.ToolUseBlock(block.path("id").asText(), block.path("name").asText(), input));
            }
        }
        return new AgentLoop.ModelResponse(blocks);
    }

    /** 把普通文本、模型响应块或 tool_result Map 转为 API content。 */
    private static JsonNode contentNode(Object content) {
        if (content instanceof String text) return JSON.getNodeFactory().textNode(text);
        ArrayNode array = JSON.createArrayNode();
        if (content instanceof List<?> values) for (Object value : values) {
            if (value instanceof AgentLoop.TextBlock text) {
                ObjectNode node = array.addObject(); node.put("type", "text"); node.put("text", text.text());
            } else if (value instanceof AgentLoop.ToolUseBlock tool) {
                ObjectNode node = array.addObject(); node.put("type", "tool_use"); node.put("id", tool.id());
                node.put("name", tool.name()); node.set("input", JSON.valueToTree(tool.input()));
            } else array.add(JSON.valueToTree(value));
        }
        return array;
    }

    /** 定义模型可见的四种工具及 JSON Schema。 */
    private static ArrayNode toolDefinitions() {
        ArrayNode tools = JSON.createArrayNode();
        addTool(tools, "glob", "Find workspace files matching a glob pattern.",
                "{\"type\":\"object\",\"properties\":{\"pattern\":{\"type\":\"string\"}},\"required\":[\"pattern\"]}");
        addTool(tools, "read_file", "Read a range of lines from a workspace file.",
                "{\"type\":\"object\",\"properties\":{\"path\":{\"type\":\"string\"},\"offset\":{\"type\":\"integer\"},\"limit\":{\"type\":\"integer\"}},\"required\":[\"path\"]}");
        addTool(tools, "apply_patch", "Replace text exactly once; empty old_text only creates a file.",
                "{\"type\":\"object\",\"properties\":{\"path\":{\"type\":\"string\"},\"old_text\":{\"type\":\"string\"},\"new_text\":{\"type\":\"string\"}},\"required\":[\"path\",\"old_text\",\"new_text\"]}");
        addTool(tools, "bash", "Run one argv program in the sandbox; shell operators are unsupported.",
                "{\"type\":\"object\",\"properties\":{\"argv\":{\"type\":\"array\",\"items\":{\"type\":\"string\"}},\"timeout\":{\"type\":\"integer\"}},\"required\":[\"argv\"]}");
        return tools;
    }

    private static void addTool(ArrayNode tools, String name, String description, String schema) {
        try {
            ObjectNode tool = tools.addObject(); tool.put("name", name); tool.put("description", description);
            tool.set("input_schema", JSON.readTree(schema));
        } catch (Exception impossible) { throw new IllegalStateException(impossible); }
    }
}
