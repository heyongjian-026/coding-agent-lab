package com.hyj.codingagent;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.hyj.codingagent.Planning.CompletionCheck;
import com.hyj.codingagent.Planning.TaskPlan;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 模型与工具之间的主循环。
 *
 * <p>循环负责维护消息配对、资源上限、API 重试、上下文压缩、计划推进与有限返工；
 * 真正的文件和命令权限仍由 {@link ToolRuntime} 负责。</p>
 */
public final class AgentLoop {
    private static final ObjectMapper JSON = new ObjectMapper();

    /** 单次请求的资源与返工上限。 */
    public record Limits(int maxRounds, int maxToolCalls, int maxContextChars, int maxTokens,
                         int maxReworkCycles, int replanFailureThreshold) {
        public static Limits defaults() { return new Limits(30, 80, 120_000, 8_000, 2, 2); }
    }

    /** 模型响应块的最小领域协议，隔离具体模型 SDK。 */
    public sealed interface Block permits TextBlock, ToolUseBlock {}
    public record TextBlock(String text) implements Block {}
    public record ToolUseBlock(String id, String name, Map<String, Object> input) implements Block {}
    public record Message(String role, Object content) {}
    /** 发给模型适配器的请求，不暴露 Anthropic 专用类型。 */
    public record ModelRequest(String model, String system, List<Message> messages, int maxTokens) {}
    public record ModelResponse(List<Block> content) {}

    @FunctionalInterface
    /** 可替换模型接口；测试使用脚本模型，CLI 使用 HTTP 实现。 */
    public interface ModelClient { ModelResponse create(ModelRequest request) throws Exception; }

    /** 循环终止时交给 CLI 或上层服务的结构化报告。 */
    public static final class Report {
        public String finalText = "";
        public String stoppedReason = "completed";
        public int reworkCycles;
        public TaskPlan plan;
        public CompletionCheck completionCheck;
        public final List<String> remainingIssues = new ArrayList<>();
        public List<String> changedFiles = List.of();
        public List<String> verification = List.of();
        public List<String> completedActions = List.of();
        public List<String> recoverySuggestions = List.of();
    }

    private AgentLoop() {}

    /**
     * 执行一次完整用户请求，直到模型给出通过验收的文本、达到上限或发生 API 错误。
     */
    public static Report run(ModelClient client, List<Message> messages, ToolRuntime runtime, String model, Limits limits) {
        Report report = new Report();
        int toolCalls = 0;
        String requestText = latestUserRequest(messages);
        TaskPlan plan = Planning.createTaskPlan(requestText);
        report.plan = plan;

        for (int round = 0; round < limits.maxRounds(); round++) {
            // 压缩发生在请求模型前，确保实际发送内容不持续无界增长。
            compactInPlace(messages, limits.maxContextChars());
            ModelResponse response;
            try {
                response = withRetry(() -> client.create(new ModelRequest(model,
                        Planning.buildSystemPrompt(runtime.workspace(), plan), List.copyOf(messages), limits.maxTokens())), 4);
            } catch (Exception error) {
                report.stoppedReason = "api_error";
                report.remainingIssues.add("API error: " + error.getMessage());
                break;
            }
            messages.add(new Message("assistant", response.content()));
            List<ToolUseBlock> tools = response.content().stream().filter(ToolUseBlock.class::isInstance)
                    .map(ToolUseBlock.class::cast).toList();
            // 没有 tool_use 只代表模型想结束；仍必须通过独立完成门禁。
            if (tools.isEmpty()) {
                report.finalText = response.content().stream().filter(TextBlock.class::isInstance)
                        .map(TextBlock.class::cast).map(TextBlock::text).reduce((a, b) -> a + "\n" + b).orElse("");
                plan.completeTerminalStep(!WorkspaceTools.verificationCommands(runtime.workspace()).isEmpty());
                report.completionCheck = Planning.checkCompletion(requestText, plan, runtime);
                if (report.completionCheck.passed()) break;
                if (report.reworkCycles >= limits.maxReworkCycles()) {
                    report.stoppedReason = "rework_limit";
                    report.remainingIssues.addAll(report.completionCheck.issues());
                    break;
                }
                report.reworkCycles++;
                plan.prepareRework(report.completionCheck.issues());
                plan.replanRemaining("completion criteria failed");
                messages.add(new Message("user", "Completion check failed. Continue working and address: "
                        + String.join("; ", report.completionCheck.issues())));
                continue;
            }

            List<Map<String, Object>> results = new ArrayList<>();
            for (ToolUseBlock tool : tools) {
                String output;
                if (toolCalls >= limits.maxToolCalls()) {
                    report.stoppedReason = "tool_limit";
                    if (!report.remainingIssues.contains("Maximum tool-call limit reached"))
                        report.remainingIssues.add("Maximum tool-call limit reached");
                    output = "Error[tool_limit]: tool call was not executed";
                } else {
                    toolCalls++;
                    output = runtime.execute(tool.name(), tool.input());
                    plan.recordToolResult(tool.name(), output);
                    var failed = plan.stepForTool(tool.name());
                    if (failed != null && failed.status() == Planning.StepStatus.FAILED
                            && failed.attempts() >= limits.replanFailureThreshold()) {
                        plan.replanRemaining("repeated " + tool.name() + " failure");
                    } else if (failed != null && failed.status() == Planning.StepStatus.FAILED) {
                        plan.replanRemaining("local ReAct retry after " + tool.name() + " failure");
                    }
                }
                // 即使超限或失败，也为每个 tool_use 构造对应 tool_result，保持协议配对。
                Map<String, Object> result = new LinkedHashMap<>();
                result.put("type", "tool_result"); result.put("tool_use_id", tool.id()); result.put("content", output);
                results.add(result);
            }
            messages.add(new Message("user", results));
            if (report.stoppedReason.equals("tool_limit")) break;
            if (round + 1 == limits.maxRounds()) {
                report.stoppedReason = "round_limit";
                report.remainingIssues.add("Maximum agent-round limit reached");
            }
        }
        report.changedFiles = runtime.changedFiles().stream().sorted().toList();
        report.verification = runtime.verification();
        report.completedActions = runtime.completedActions();
        report.recoverySuggestions = runtime.recoverySuggestions();
        return report;
    }

    @FunctionalInterface private interface ThrowingSupplier<T> { T get() throws Exception; }

    /** 只重试限流和服务过载等瞬时 API 错误；业务错误立即抛出。 */
    static <T> T withRetry(ThrowingSupplier<T> operation, int attempts) throws Exception {
        Exception last = null;
        for (int index = 0; index < attempts; index++) {
            try { return operation.get(); }
            catch (Exception error) {
                String text = (error.getClass().getSimpleName() + " " + error.getMessage()).toLowerCase();
                if (!(text.contains("429") || text.contains("529") || text.contains("ratelimit") || text.contains("overloaded"))) throw error;
                last = error;
                if (index + 1 < attempts) Thread.sleep(Math.min(1L << index, 8L) * 10L);
            }
        }
        throw new IllegalStateException("Transient API error after " + attempts + " attempts: " + last);
    }

    /** 超过字符预算时丢弃较早消息，保留最近工具状态并插入明确压缩标记。 */
    static void compactInPlace(List<Message> messages, int maxChars) {
        if (characters(messages) <= maxChars) return;
        int keepFrom = Math.max(1, messages.size() - 2);
        while (keepFrom > 1 && characters(messages.subList(keepFrom - 1, messages.size())) < maxChars) keepFrom--;
        List<Message> recent = new ArrayList<>(messages.subList(keepFrom, messages.size()));
        messages.clear();
        messages.add(new Message("user", "[Context compacted: earlier messages were removed. Continue from the recent tool state.]"));
        messages.addAll(recent);
    }

    private static int characters(List<Message> messages) {
        try { return JSON.writeValueAsString(messages).length(); }
        catch (Exception error) { return messages.toString().length(); }
    }

    private static String latestUserRequest(List<Message> messages) {
        for (int index = messages.size() - 1; index >= 0; index--) {
            Message message = messages.get(index);
            if (message.role().equals("user") && message.content() instanceof String text) return text;
        }
        return "";
    }
}
