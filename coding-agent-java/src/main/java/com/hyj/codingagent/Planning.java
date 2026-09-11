package com.hyj.codingagent;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Set;
import java.util.regex.Pattern;

/**
 * 混合执行架构与独立完成条件检查。
 *
 * <p>简单任务只有一个 ReAct 步骤；复杂修改任务先建立 inspect/implement/verify/review
 * 计划，但每个步骤内部仍允许模型根据工具结果进行 ReAct 调整。</p>
 */
public final class Planning {
    private static final Pattern CHANGE = Pattern.compile(
            "\\b(add|build|change|create|delete|edit|fix|implement|refactor|remove|update)\\b|增加|修改|修复|实现|重构|删除|创建|完成",
            Pattern.CASE_INSENSITIVE);
    private static final Pattern COMPLEX = Pattern.compile(
            "\\b(and|then|multiple|refactor|architecture|workflow)\\b|并且|然后|多个|架构|工作流|逐步|重构",
            Pattern.CASE_INSENSITIVE);

    private Planning() {}

    /** 计划步骤的有限状态集合。 */
    public enum StepStatus { PENDING, IN_PROGRESS, COMPLETED, FAILED }

    /** 单个计划步骤及其尝试次数和工具证据。 */
    public static final class PlanStep {
        private final int id;
        private String title;
        private final String kind;
        private StepStatus status = StepStatus.PENDING;
        private int attempts;
        private final List<String> evidence = new ArrayList<>();

        public PlanStep(int id, String title, String kind) { this.id = id; this.title = title; this.kind = kind; }
        public int id() { return id; }
        public String title() { return title; }
        public String kind() { return kind; }
        public StepStatus status() { return status; }
        public int attempts() { return attempts; }
        public List<String> evidence() { return List.copyOf(evidence); }
    }

    /** 一次用户请求的执行计划，包含状态推进、失败事件与重规划次数。 */
    public static final class TaskPlan {
        private final String originalRequest;
        private final String mode;
        private final List<PlanStep> steps;
        private final List<String> events = new ArrayList<>();
        private int replans;

        TaskPlan(String originalRequest, String mode, List<PlanStep> steps) {
            this.originalRequest = originalRequest; this.mode = mode; this.steps = steps;
            startNext();
        }

        /** 返回第一个尚未完成的步骤。 */
        public PlanStep currentStep() {
            return steps.stream().filter(step -> step.status == StepStatus.PENDING || step.status == StepStatus.IN_PROGRESS)
                    .findFirst().orElse(null);
        }

        /** 将下一个 pending 步骤推进到 in-progress。 */
        public PlanStep startNext() {
            PlanStep step = currentStep();
            if (step != null && step.status == StepStatus.PENDING) step.status = StepStatus.IN_PROGRESS;
            return step;
        }

        /** 将具体工具映射到 inspect、implement 或 verify 计划步骤。 */
        public PlanStep stepForTool(String tool) {
            String kind = switch (tool) {
                case "glob", "read_file" -> "inspect";
                case "apply_patch" -> "implement";
                case "bash" -> "verify";
                default -> "execute";
            };
            return steps.stream().filter(step -> step.kind.equals(kind) && step.status != StepStatus.COMPLETED)
                    .findFirst().orElse(currentStep());
        }

        /** 根据工具成功或失败更新步骤状态并保存证据。 */
        public void recordToolResult(String tool, String output) {
            PlanStep step = stepForTool(tool);
            if (step == null) return;
            step.attempts++;
            String failure = CommandSupport.classifyFailure(output);
            if (failure != null) {
                step.status = StepStatus.FAILED;
                step.evidence.add(tool + ": " + failure);
                events.add("step " + step.id + " failed; ReAct adjustment requested");
            } else {
                step.status = StepStatus.COMPLETED;
                step.evidence.add(tool + ": success");
                startNext();
            }
        }

        /**
         * 保留已完成证据，只把失败步骤改写为替代方案重试，属于轻量级重新规划。
         */
        public void replanRemaining(String reason) {
            replans++;
            events.add("replanned: " + reason);
            steps.stream().filter(step -> step.status == StepStatus.FAILED).forEach(step -> {
                step.status = StepStatus.IN_PROGRESS;
                if (!step.title.startsWith("Retry")) step.title = "Retry with an alternative approach: " + step.title;
            });
            startNext();
        }

        /** 模型返回最终文本时，完成无需命令的验证步骤及 review/execute 终结步骤。 */
        public void completeTerminalStep(boolean hasVerificationCommand) {
            PlanStep current = currentStep();
            if (current != null && current.kind.equals("verify") && !hasVerificationCommand) {
                current.status = StepStatus.COMPLETED;
                current.evidence.add("no configured verification command");
                current = startNext();
            }
            if (current != null && Set.of("review", "execute").contains(current.kind)) {
                current.status = StepStatus.COMPLETED;
                current.evidence.add("model returned final review");
            }
        }

        /** 根据完成门禁问题，把计划定位回实现、验证或复核阶段。 */
        public void prepareRework(List<String> issues) {
            events.add("completion check failed: " + String.join("; ", issues));
            List<String> kinds = new ArrayList<>();
            if (issues.stream().anyMatch(value -> value.contains("change request"))) kinds.addAll(List.of("implement", "execute"));
            if (issues.stream().anyMatch(value -> value.contains("verification"))) kinds.addAll(List.of("verify", "execute"));
            if (issues.stream().anyMatch(value -> value.contains("changed file"))) kinds.addAll(List.of("review", "inspect"));
            for (String kind : kinds) {
                PlanStep step = steps.stream().filter(value -> value.kind.equals(kind)).findFirst().orElse(null);
                if (step != null) { step.status = StepStatus.IN_PROGRESS; return; }
            }
            PlanStep step = steps.stream().filter(value -> value.status != StepStatus.COMPLETED).findFirst().orElse(null);
            if (step != null) step.status = StepStatus.IN_PROGRESS;
        }

        public String mode() { return mode; }
        public List<PlanStep> steps() { return List.copyOf(steps); }
        public List<String> events() { return List.copyOf(events); }
        public int replans() { return replans; }
    }

    /** 独立于模型自述的验收结果。 */
    public record CompletionCheck(boolean passed, Map<String, Boolean> checks, List<String> issues) {}

    /** 用可解释的启发式规则选择 ReAct 或 Plan-and-Execute。 */
    public static String selectExecutionMode(String request) {
        int score = request.length() >= 100 ? 1 : 0;
        if (COMPLEX.matcher(request).find()) score++;
        if (request.lines().count() >= 3) score++;
        if (Pattern.compile("(?:^|\\s)\\d+[.)、]").matcher(request).results().count() >= 2) score++;
        return score >= 1 ? "plan_and_execute" : "react";
    }

    /** 为请求建立初始结构化计划，并立即启动第一步。 */
    public static TaskPlan createTaskPlan(String request) {
        String mode = selectExecutionMode(request);
        List<PlanStep> steps = new ArrayList<>();
        if (mode.equals("react")) {
            steps.add(new PlanStep(1, "Inspect and complete the request with ReAct", "execute"));
        } else {
            steps.add(new PlanStep(1, "Inspect relevant context", "inspect"));
            if (changeRequested(request)) {
                steps.add(new PlanStep(steps.size() + 1, "Implement the requested change", "implement"));
                steps.add(new PlanStep(steps.size() + 1, "Run relevant verification", "verify"));
            }
            steps.add(new PlanStep(steps.size() + 1, "Review the result against the request", "review"));
        }
        return new TaskPlan(request, mode, steps);
    }

    /**
     * 使用运行时事实检查需求覆盖、变更范围、验证证据和计划状态。
     * 模型说“完成”不会绕过这里的检查。
     */
    public static CompletionCheck checkCompletion(String request, TaskPlan plan, ToolRuntime runtime) {
        boolean changeRequested = changeRequested(request);
        boolean requestCovered = !changeRequested || !runtime.changedFiles().isEmpty();
        boolean scopedDiff = union(runtime.inspectedFiles(), runtime.createdFiles()).containsAll(runtime.changedFiles());
        boolean verificationNeeded = changeRequested && !WorkspaceTools.verificationCommands(runtime.workspace()).isEmpty();
        boolean verificationOk = !verificationNeeded || runtime.verification().stream().anyMatch(value -> value.contains("[exit 0]"));
        boolean planComplete = plan.steps.stream().allMatch(step -> step.status == StepStatus.COMPLETED);
        Map<String, Boolean> checks = new LinkedHashMap<>();
        checks.put("request_covered", requestCovered);
        checks.put("no_unrelated_diff", scopedDiff);
        checks.put("verification_evidence", verificationOk);
        checks.put("plan_complete", planComplete);
        Map<String, String> labels = Map.of(
                "request_covered", "No approved file change proves the change request was covered",
                "no_unrelated_diff", "A changed file was neither inspected first nor created by this task",
                "verification_evidence", "Required verification has no successful tool evidence",
                "plan_complete", "One or more planned steps are incomplete");
        List<String> issues = checks.entrySet().stream().filter(entry -> !entry.getValue())
                .map(entry -> labels.get(entry.getKey())).toList();
        return new CompletionCheck(issues.isEmpty(), Map.copyOf(checks), issues);
    }

    /** 把当前执行模式和实时计划状态注入下一轮模型请求。 */
    public static String buildSystemPrompt(java.nio.file.Path workspace, TaskPlan plan) {
        String steps = plan.steps.stream().map(step -> step.id + ". " + step.title + " [" + step.status.name().toLowerCase(Locale.ROOT) + "]")
                .reduce((a, b) -> a + "; " + b).orElse("");
        return "You are a careful CLI coding agent. Your workspace is " + workspace.toAbsolutePath().normalize()
                + ". Inspect relevant files before editing. Use glob and read_file, apply minimal patches, then run relevant tests or builds. "
                + "Never claim verification succeeded unless a tool result proves it. Respect denied operations. Execution mode: "
                + plan.mode + ". Current plan: " + steps + ".";
    }

    public static boolean changeRequested(String request) { return CHANGE.matcher(request).find(); }

    private static <T> Set<T> union(Set<T> left, Set<T> right) {
        java.util.HashSet<T> result = new java.util.HashSet<>(left); result.addAll(right); return result;
    }
}
