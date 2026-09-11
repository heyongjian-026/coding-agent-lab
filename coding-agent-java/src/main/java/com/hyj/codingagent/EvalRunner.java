package com.hyj.codingagent;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.SerializationFeature;

import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;

/**
 * 确定性离线轨迹回放评测器。
 *
 * <p>数据集直接给出工具轨迹，因此它评估的是 Harness、权限和验收逻辑的回归，
 * 不是模型自主选择工具的真实准确率。真实模型评测应另建非确定性数据集。</p>
 */
public final class EvalRunner {
    private static final ObjectMapper JSON = new ObjectMapper().enable(SerializationFeature.INDENT_OUTPUT);
    private static final Set<String> REQUIRED_CATEGORIES = Set.of("bug_fix", "feature", "test_repair", "safety");

    /** 单条案例的原始评测结果，之后由 calculateMetrics 聚合。 */
    public record CaseResult(String id, String category, boolean completed, Boolean testsPassed,
                             boolean unrelatedModification, boolean toolSelectionCorrect,
                             int validArguments, int toolCalls, int repairRounds,
                             Boolean dangerousActionBlocked, List<String> failures) {}

    private EvalRunner() {}

    @SuppressWarnings("unchecked")
    /**
     * 建立案例初始文件，顺序回放 trajectory，再按照 acceptance 验收最终状态。
     */
    public static CaseResult runCase(Map<String, Object> testCase, Path workspace) throws Exception {
        Map<String, String> initial = (Map<String, String>) testCase.getOrDefault("initial_files", Map.of());
        for (var entry : initial.entrySet()) {
            Path target = workspace.resolve(entry.getKey());
            if (target.getParent() != null) Files.createDirectories(target.getParent());
            Files.writeString(target, entry.getValue(), StandardCharsets.UTF_8);
        }
        List<Boolean> approvals = (List<Boolean>) testCase.getOrDefault("approvals", List.of());
        int[] approvalIndex = {0};
        ToolRuntime.Approver approver = ignored -> approvalIndex[0] >= approvals.size() || approvals.get(approvalIndex[0]++);
        // 离线评测只允许固定 pytest 命令，避免数据文件任意启动其他程序。
        CommandSupport.SandboxBackend sandbox = (request, root) -> {
            if (!request.argv().equals(List.of("python", "-m", "pytest", "-q")))
                return new CommandSupport.CommandResult(CommandSupport.show(request.argv()), -1, "", "evaluation command is not allowlisted", false);
            return new CommandSupport.LocalBackend().execute(request, root);
        };
        ToolRuntime runtime = new ToolRuntime(workspace, approver, sandbox);
        List<String> outputs = new ArrayList<>();
        List<String> observedTools = new ArrayList<>();
        List<Boolean> verification = new ArrayList<>();
        int validArguments = 0, repairRounds = 0;
        // observedTools 来自固定轨迹；该指标用于发现评测数据或回放器协议漂移。
        for (Map<String, Object> action : (List<Map<String, Object>>) testCase.get("trajectory")) {
            String tool = (String) action.get("tool");
            observedTools.add(tool);
            String output = runtime.execute(tool, (Map<String, Object>) action.getOrDefault("args", Map.of()));
            outputs.add(output);
            String failure = CommandSupport.classifyFailure(output);
            boolean argumentError = output.startsWith("Error: IllegalArgumentException")
                    && !output.contains("Refusing to access sensitive file");
            if (!"invalid_arguments".equals(failure) && !argumentError) validArguments++;
            if (failure != null) repairRounds++;
            Object argv = ((Map<String, Object>) action.getOrDefault("args", Map.of())).get("argv");
            if (tool.equals("bash") && argv instanceof List<?> values && values.contains("pytest")) verification.add(output.contains("[exit 0]"));
        }
        // 工具调用完成后才验收文件、输出、变更范围、测试和安全拦截。
        Map<String, Object> acceptance = (Map<String, Object>) testCase.get("acceptance");
        List<String> failures = new ArrayList<>();
        Map<String, String> expectedFiles = (Map<String, String>) acceptance.getOrDefault("files_equal", Map.of());
        for (var entry : expectedFiles.entrySet()) {
            Path path = workspace.resolve(entry.getKey());
            String actual = Files.isRegularFile(path) ? Files.readString(path) : null;
            if (!java.util.Objects.equals(actual, entry.getValue())) failures.add("file mismatch: " + entry.getKey());
        }
        for (String expected : (List<String>) acceptance.getOrDefault("outputs_contain", List.of())) {
            if (outputs.stream().noneMatch(output -> output.toLowerCase().contains(expected.toLowerCase())))
                failures.add("missing output: " + expected);
        }
        Set<String> expectedChanged = new HashSet<>((List<String>) acceptance.getOrDefault("changed_files", List.of()));
        boolean unrelated = !expectedChanged.containsAll(runtime.changedFiles());
        if (unrelated) failures.add("unrelated modification");
        Boolean testsPassed = verification.isEmpty() ? null : verification.stream().allMatch(Boolean::booleanValue);
        if (Boolean.TRUE.equals(acceptance.get("tests_must_pass")) && !Boolean.TRUE.equals(testsPassed)) failures.add("tests did not pass");
        Boolean dangerousBlocked = null;
        if (acceptance.containsKey("dangerous_action_blocked")) {
            dangerousBlocked = outputs.stream().anyMatch(output -> output.startsWith("Denied"));
            if (!dangerousBlocked.equals(acceptance.get("dangerous_action_blocked"))) failures.add("dangerous action was not blocked");
        }
        return new CaseResult((String) testCase.get("id"), (String) testCase.get("category"), failures.isEmpty(),
                testsPassed, unrelated, observedTools.equals(testCase.get("expected_tools")), validArguments,
                observedTools.size(), repairRounds, dangerousBlocked, failures);
    }

    /** 聚合任务质量、工具协议、修复成本和安全性七项指标。 */
    public static Map<String, Double> calculateMetrics(List<CaseResult> results) {
        double total = results.isEmpty() ? 1 : results.size();
        List<Boolean> tests = results.stream().map(CaseResult::testsPassed).filter(java.util.Objects::nonNull).toList();
        List<Boolean> safety = results.stream().map(CaseResult::dangerousActionBlocked).filter(java.util.Objects::nonNull).toList();
        int calls = Math.max(1, results.stream().mapToInt(CaseResult::toolCalls).sum());
        Map<String, Double> metrics = new LinkedHashMap<>();
        metrics.put("task_completion_rate", results.stream().filter(CaseResult::completed).count() / total);
        metrics.put("test_pass_rate", tests.isEmpty() ? 1.0 : tests.stream().filter(Boolean::booleanValue).count() / (double) tests.size());
        metrics.put("unrelated_modification_rate", results.stream().filter(CaseResult::unrelatedModification).count() / total);
        metrics.put("tool_selection_accuracy", results.stream().filter(CaseResult::toolSelectionCorrect).count() / total);
        metrics.put("argument_accuracy", results.stream().mapToInt(CaseResult::validArguments).sum() / (double) calls);
        metrics.put("average_repair_rounds", results.stream().mapToInt(CaseResult::repairRounds).sum() / total);
        metrics.put("dangerous_action_block_rate", safety.isEmpty() ? 1.0 : safety.stream().filter(Boolean::booleanValue).count() / (double) safety.size());
        return metrics;
    }

    @SuppressWarnings("unchecked")
    /**
     * 与版本化基线比较；修改率和修复轮数越低越好，其余指标越高越好。
     */
    public static List<String> compareBaseline(Map<String, Double> metrics, Map<String, Object> baseline) {
        Set<String> lowerBetter = Set.of("unrelated_modification_rate", "average_repair_rounds");
        Map<String, Number> expected = (Map<String, Number>) baseline.get("metrics");
        Map<String, Number> tolerances = (Map<String, Number>) baseline.getOrDefault("tolerances", Map.of());
        List<String> regressions = new ArrayList<>();
        expected.forEach((name, value) -> {
            double actual = metrics.get(name), target = value.doubleValue();
            double tolerance = tolerances.getOrDefault(name, 0.0).doubleValue();
            boolean regressed = lowerBetter.contains(name) ? actual > target + tolerance : actual < target - tolerance;
            if (regressed) regressions.add("%s: actual=%.4f, baseline=%.4f".formatted(name, actual, target));
        });
        return regressions;
    }

    @SuppressWarnings("unchecked")
    /** 校验数据集、隔离运行所有案例、生成 JSON 报告并返回退化列表。 */
    public static Map<String, Object> runSuite(Path datasetPath, Path outputPath, Path baselinePath) throws Exception {
        Map<String, Object> dataset = JSON.readValue(datasetPath.toFile(), new TypeReference<>() {});
        List<Map<String, Object>> cases = (List<Map<String, Object>>) dataset.getOrDefault("cases", List.of());
        if (cases.size() < 10 || cases.size() > 20) throw new IllegalArgumentException("offline dataset must contain 10 to 20 cases");
        Set<String> categories = new HashSet<>(); cases.forEach(value -> categories.add((String) value.get("category")));
        if (!categories.containsAll(REQUIRED_CATEGORIES)) throw new IllegalArgumentException("dataset must cover all required categories");
        List<CaseResult> results = new ArrayList<>();
        // 每条案例使用独立临时工作区，避免案例之间通过文件状态互相污染。
        Path root = Files.createTempDirectory("coding-agent-eval-");
        try {
            for (int i = 0; i < cases.size(); i++) {
                Path workspace = Files.createDirectory(root.resolve("%02d-%s".formatted(i, cases.get(i).get("id"))));
                results.add(runCase(cases.get(i), workspace));
            }
        } finally { deleteTree(root); }
        Map<String, Double> metrics = calculateMetrics(results);
        List<String> regressions = baselinePath == null ? List.of()
                : compareBaseline(metrics, JSON.readValue(baselinePath.toFile(), new TypeReference<>() {}));
        Map<String, Object> report = new LinkedHashMap<>();
        report.put("schema_version", 1); report.put("suite", dataset.get("suite")); report.put("case_count", results.size());
        report.put("metrics", metrics); report.put("cases", results); report.put("regressions", regressions);
        if (outputPath.getParent() != null) Files.createDirectories(outputPath.getParent());
        JSON.writeValue(outputPath.toFile(), report);
        return report;
    }

    /** CLI 入口；检测到任何基线退化时以退出码 1 结束，供 CI 阻止合并。 */
    public static void main(String[] args) throws Exception {
        Path dataset = Path.of("evals/tasks.json"), output = Path.of("evals/latest-results.json"), baseline = null;
        for (int i = 0; i < args.length; i++) {
            if (args[i].equals("--dataset")) dataset = Path.of(args[++i]);
            else if (args[i].equals("--output")) output = Path.of(args[++i]);
            else if (args[i].equals("--baseline")) baseline = Path.of(args[++i]);
        }
        Map<String, Object> report = runSuite(dataset, output, baseline);
        System.out.println(JSON.writeValueAsString(report.get("metrics")));
        if (!((List<?>) report.get("regressions")).isEmpty()) System.exit(1);
    }

    private static void deleteTree(Path root) throws Exception {
        if (!Files.exists(root)) return;
        try (var stream = Files.walk(root)) {
            for (Path path : stream.sorted(java.util.Comparator.reverseOrder()).toList()) Files.deleteIfExists(path);
        }
    }
}
