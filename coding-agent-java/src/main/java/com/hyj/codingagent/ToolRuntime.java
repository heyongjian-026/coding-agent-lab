package com.hyj.codingagent;

import com.hyj.codingagent.CommandSupport.ApprovalTicket;
import com.hyj.codingagent.CommandSupport.CommandPolicy;
import com.hyj.codingagent.CommandSupport.CommandRequest;
import com.hyj.codingagent.CommandSupport.PolicyDecision;
import com.hyj.codingagent.CommandSupport.SandboxBackend;

import java.nio.file.Path;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

/**
 * 四种内置工具的统一运行时。
 *
 * <p>它负责审批、策略、审计、状态跟踪和熔断，而不是让每个工具各自实现一套安全逻辑。
 * changed/inspected/created/verification 等集合也会成为完成条件检查器的客观证据。</p>
 */
public final class ToolRuntime {
    /** 把用户交互从运行时解耦，测试可以注入固定批准或拒绝。 */
    @FunctionalInterface
    public interface Approver { boolean approve(String prompt); }

    private final Path workspace;
    private final Approver approver;
    private final SessionLogger logger;
    private final CommandPolicy policy;
    private final SandboxBackend sandbox;
    private final int circuitBreakerThreshold;
    private final int verificationFailureLimit;
    private final Map<String, FailureCount> consecutiveFailures = new LinkedHashMap<>();
    private final Map<String, Integer> commandFailures = new LinkedHashMap<>();
    private final Set<String> openCircuits = new LinkedHashSet<>();
    private final Set<String> changedFiles = new LinkedHashSet<>();
    private final Set<String> inspectedFiles = new LinkedHashSet<>();
    private final Set<String> createdFiles = new LinkedHashSet<>();
    private final List<String> verification = new ArrayList<>();
    private final List<String> completedActions = new ArrayList<>();
    private final List<String> recoverySuggestions = new ArrayList<>();

    private record FailureCount(String kind, int count) {}

    /** 使用默认熔断阈值创建一次会话的工具运行时。 */
    public ToolRuntime(Path workspace, Approver approver, SandboxBackend sandbox) {
        this(workspace, approver, sandbox, null, 3, 2);
    }

    /** 完整构造器，允许测试控制工具和相同命令的失败阈值。 */
    public ToolRuntime(Path workspace, Approver approver, SandboxBackend sandbox,
                       SessionLogger logger, int circuitBreakerThreshold, int verificationFailureLimit) {
        this.workspace = workspace.toAbsolutePath().normalize();
        this.approver = approver;
        this.sandbox = sandbox;
        this.logger = logger == null ? new SessionLogger(this.workspace) : logger;
        this.policy = new CommandPolicy(this.workspace);
        this.circuitBreakerThreshold = circuitBreakerThreshold;
        this.verificationFailureLimit = verificationFailureLimit;
    }

    /** 清理单次用户请求的执行状态，但保留会话日志文件。 */
    public void resetTurn() {
        changedFiles.clear(); inspectedFiles.clear(); createdFiles.clear(); verification.clear();
        consecutiveFailures.clear(); commandFailures.clear(); openCircuits.clear();
        completedActions.clear(); recoverySuggestions.clear();
    }

    /**
     * 执行一次结构化工具调用，并保证成功、拒绝或异常都转换为可回传给 LLM 的文本结果。
     */
    public String execute(String name, Map<String, Object> args) {
        logger.log("tool_start", Map.of("name", name, "args", args));
        // 熔断后不再触碰真实工具，但仍返回 tool_result，让模型有机会选择其他路径。
        if (openCircuits.contains(name)) {
            return logResult(name, "Error[circuit_open]: repeated failures disabled tool '" + name + "' for this turn");
        }
        String output;
        try {
            output = switch (name) {
                case "glob" -> WorkspaceTools.glob(workspace, requiredString(args, "pattern"));
                case "read_file" -> readFile(args);
                case "apply_patch" -> applyPatch(args);
                case "bash" -> runCommand(args);
                default -> "Error: unknown tool '" + name + "'";
            };
        } catch (Exception error) {
            output = "Error: " + error.getClass().getSimpleName() + ": " + error.getMessage();
        }
        recordOutcome(name, output);
        return logResult(name, output);
    }

    /** 读取文件、记录检查证据，并把注入检测结果以前缀警告返回给模型。 */
    private String readFile(Map<String, Object> args) throws Exception {
        String path = requiredString(args, "path");
        String output = WorkspaceTools.readFile(workspace, path, integer(args, "offset", 0), integer(args, "limit", 400));
        inspectedFiles.add(normalizeRelative(path));
        List<String> warnings = WorkspaceTools.detectPromptInjection(output);
        if (!warnings.isEmpty()) {
            logger.log("injection_warning", Map.of("path", path, "matches", warnings));
            return "[Security warning: untrusted file contains instruction-like text]\n" + output;
        }
        return output;
    }

    /** 执行 Patch 的预览、人工审批、原文指纹复核和状态登记。 */
    private String applyPatch(Map<String, Object> args) throws Exception {
        String path = requiredString(args, "path");
        String oldText = requiredString(args, "old_text");
        String newText = requiredString(args, "new_text");
        WorkspaceTools.PatchPreview preview = WorkspaceTools.previewPatch(workspace, path, oldText, newText);
        boolean allowed = approver.approve("Apply this patch?\n" + preview.diff());
        logger.log("approval", Map.of("operation", "apply_patch", "allowed", allowed));
        if (!allowed) return "Denied by user";
        String output = WorkspaceTools.applyPatch(workspace, path, preview);
        String normalized = normalizeRelative(path);
        changedFiles.add(normalized);
        if (preview.original().isEmpty()) createdFiles.add(normalized);
        return output;
    }

    @SuppressWarnings("unchecked")
    /**
     * 解析 argv，执行相同命令熔断、确定性策略、人工审批和沙箱调用。
     */
    private String runCommand(Map<String, Object> args) {
        Object raw = args.get("argv");
        if (!(raw instanceof List<?> values)) throw new IllegalArgumentException("argv must be an array");
        List<String> argv = values.stream().map(value -> {
            if (!(value instanceof String text)) throw new IllegalArgumentException("argv values must be strings");
            return text;
        }).toList();
        CommandRequest request = new CommandRequest(argv, integer(args, "timeout", 60));
        String key = String.join("\u0000", argv);
        // 命令级限制比工具级熔断更细：防止模型反复运行同一个失败测试。
        if (commandFailures.getOrDefault(key, 0) >= verificationFailureLimit) {
            openCircuits.add("bash");
            return "Error[circuit_open]: repeated command failures reached the repair limit";
        }
        PolicyDecision decision = policy.evaluate(request);
        logger.log("policy_decision", Map.of("action", decision.action().name().toLowerCase(),
                "reason", decision.reason(), "risks", decision.risks()));
        if (decision.action() == PolicyDecision.Action.DENY) return "Denied: " + decision.reason();
        // 审批前冻结指纹，审批后再次 verify，防止参数在两步之间变化。
        ApprovalTicket ticket = ApprovalTicket.create(request);
        boolean allowed = approver.approve("Run sandboxed command? " + CommandSupport.show(argv) + "\nRisk: " + decision.reason());
        logger.log("approval", Map.of("operation", "bash", "allowed", allowed, "fingerprint", ticket.fingerprint()));
        if (!allowed) return "Denied by user";
        ticket.verify(request);
        String output = sandbox.execute(request, workspace).render();
        if (CommandSupport.isVerificationCommand(argv)) verification.add(CommandSupport.show(argv) + "\n" + output);
        if ("command_failed".equals(CommandSupport.classifyFailure(output))) {
            commandFailures.merge(key, 1, Integer::sum);
        } else {
            commandFailures.remove(key);
        }
        return output;
    }

    /** 更新工具级连续失败计数、恢复建议和开路状态。 */
    private void recordOutcome(String name, String output) {
        String failure = CommandSupport.classifyFailure(output);
        if (failure == null) {
            consecutiveFailures.remove(name);
            if (!completedActions.contains(name)) completedActions.add(name);
            return;
        }
        // 用户拒绝是正常控制流，不应因连续拒绝把工具视为故障。
        if ("permission".equals(failure)) return;
        String suggestion = name + ": " + CommandSupport.recoverySuggestion(failure);
        if (!recoverySuggestions.contains(suggestion)) recoverySuggestions.add(suggestion);
        FailureCount previous = consecutiveFailures.get(name);
        int count = previous != null && previous.kind().equals(failure) ? previous.count() + 1 : 1;
        consecutiveFailures.put(name, new FailureCount(failure, count));
        if (count >= circuitBreakerThreshold) {
            openCircuits.add(name);
            logger.log("circuit_open", Map.of("name", name, "failure", failure, "count", count));
        }
    }

    /** 发现并运行项目验证命令，同时保存可供完成门禁检查的证据。 */
    public List<String> runVerification() {
        List<String> results = new ArrayList<>();
        for (List<String> command : WorkspaceTools.verificationCommands(workspace)) {
            String output = execute("bash", Map.of("argv", command, "timeout", 120));
            results.add(CommandSupport.show(command) + "\n" + output);
        }
        verification.clear(); verification.addAll(results);
        return List.copyOf(results);
    }

    private String logResult(String name, String output) {
        logger.log("tool_result", Map.of("name", name, "output", output));
        return output;
    }

    private String normalizeRelative(String path) {
        return workspace.relativize(WorkspaceTools.resolve(workspace, path)).toString().replace('\\', '/');
    }

    private static String requiredString(Map<String, Object> args, String name) {
        Object value = args.get(name);
        if (!(value instanceof String text)) throw new IllegalArgumentException(name + " must be a string");
        return text;
    }

    private static int integer(Map<String, Object> args, String name, int fallback) {
        Object value = args.get(name);
        if (value == null) return fallback;
        if (value instanceof Number number) return number.intValue();
        throw new IllegalArgumentException(name + " must be an integer");
    }

    public Path workspace() { return workspace; }
    public Set<String> changedFiles() { return Set.copyOf(changedFiles); }
    public Set<String> inspectedFiles() { return Set.copyOf(inspectedFiles); }
    public Set<String> createdFiles() { return Set.copyOf(createdFiles); }
    public Set<String> openCircuits() { return Set.copyOf(openCircuits); }
    public List<String> verification() { return List.copyOf(verification); }
    public List<String> completedActions() { return List.copyOf(completedActions); }
    public List<String> recoverySuggestions() { return List.copyOf(recoverySuggestions); }
}
