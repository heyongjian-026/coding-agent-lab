package com.hyj.codingagent;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.concurrent.atomic.AtomicInteger;

import static org.junit.jupiter.api.Assertions.*;

class AgentLoopAndEvalTest {
    @TempDir Path workspace;

    @Test void loopPairsEveryToolUseWithResult() {
        AtomicInteger calls = new AtomicInteger();
        AgentLoop.ModelClient client = request -> calls.getAndIncrement() == 0
                ? new AgentLoop.ModelResponse(List.of(new AgentLoop.ToolUseBlock("1", "glob", Map.of("pattern", "*.java"))))
                : new AgentLoop.ModelResponse(List.of(new AgentLoop.TextBlock("done")));
        List<AgentLoop.Message> messages = new ArrayList<>(List.of(new AgentLoop.Message("user", "List Java files")));
        ToolRuntime runtime = new ToolRuntime(workspace, prompt -> true, (request, root) -> null);
        AgentLoop.Report report = AgentLoop.run(client, messages, runtime, "fake", AgentLoop.Limits.defaults());
        assertEquals("done", report.finalText);
        assertTrue(messages.stream().anyMatch(message -> message.content() instanceof List<?> list
                && !list.isEmpty() && list.get(0) instanceof Map<?, ?> map && map.get("type").equals("tool_result")));
    }

    @Test void loopReturnsToolLimitResultWithoutExecuting() {
        AgentLoop.ModelClient client = request -> new AgentLoop.ModelResponse(List.of(
                new AgentLoop.ToolUseBlock("1", "glob", Map.of("pattern", "*"))));
        List<AgentLoop.Message> messages = new ArrayList<>(List.of(new AgentLoop.Message("user", "List")));
        ToolRuntime runtime = new ToolRuntime(workspace, prompt -> true, (request, root) -> null);
        AgentLoop.Report report = AgentLoop.run(client, messages, runtime, "fake", new AgentLoop.Limits(2, 0, 1000, 10, 0, 2));
        assertEquals("tool_limit", report.stoppedReason);
        assertTrue(report.remainingIssues.contains("Maximum tool-call limit reached"));
    }

    @Test void transientApiFailureIsRetried() {
        AtomicInteger calls = new AtomicInteger();
        AgentLoop.ModelClient client = request -> {
            if (calls.getAndIncrement() == 0) throw new IllegalStateException("429 ratelimit");
            return new AgentLoop.ModelResponse(List.of(new AgentLoop.TextBlock("ok")));
        };
        var report = AgentLoop.run(client,
                new ArrayList<>(List.of(new AgentLoop.Message("user", "Explain"))),
                new ToolRuntime(workspace, prompt -> true, (request, root) -> null), "fake", AgentLoop.Limits.defaults());
        assertEquals(2, calls.get());
        assertEquals("ok", report.finalText);
    }

    @Test void contextIsCompactedAtLimit() {
        List<AgentLoop.Message> messages = new ArrayList<>();
        messages.add(new AgentLoop.Message("user", "x".repeat(100)));
        messages.add(new AgentLoop.Message("assistant", "y".repeat(100)));
        messages.add(new AgentLoop.Message("user", "latest"));
        AgentLoop.compactInPlace(messages, 80);
        assertTrue(((String) messages.get(0).content()).startsWith("[Context compacted"));
        assertEquals("latest", messages.get(messages.size() - 1).content());
    }

    @Test void runsTwelveCaseOfflineSuiteWithoutRegression() throws Exception {
        Path project = Path.of("").toAbsolutePath();
        Path output = workspace.resolve("results.json");
        Map<String, Object> report = EvalRunner.runSuite(project.resolve("evals/tasks.json"), output,
                project.resolve("evals/baseline.json"));
        assertEquals(12, report.get("case_count"));
        assertTrue(((List<?>) report.get("regressions")).isEmpty());
        assertTrue(java.nio.file.Files.exists(output));
    }

    @Test void baselineComparisonDetectsRegression() {
        Map<String, Double> metrics = Map.of("task_completion_rate", 0.5);
        Map<String, Object> baseline = Map.of("metrics", Map.of("task_completion_rate", 1.0), "tolerances", Map.of());
        assertEquals(1, EvalRunner.compareBaseline(metrics, baseline).size());
    }
}
