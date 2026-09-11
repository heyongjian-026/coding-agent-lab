package com.hyj.codingagent;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.Scanner;

/** 命令行入口：加载配置、选择沙箱、维护会话历史并展示最终报告。 */
public final class CodingAgentCli {
    private CodingAgentCli() {}

    /** 启动多轮交互式 Coding Agent。 */
    public static void main(String[] args) throws Exception {
        Map<String, String> options = parseArgs(args);
        Path workspace = Path.of(options.getOrDefault("workspace", ".")).toAbsolutePath().normalize();
        if (!Files.isDirectory(workspace)) throw new IllegalArgumentException("Workspace does not exist: " + workspace);
        Map<String, String> env = new HashMap<>(System.getenv());
        loadEnv(Path.of(".env"), env);
        String model = options.getOrDefault("model", env.get("MODEL_ID"));
        if (model == null || model.isBlank()) throw new IllegalArgumentException("Set MODEL_ID in .env or pass --model");
        String backend = options.getOrDefault("sandbox", "docker");
        // Docker 是默认安全边界；local 仅供明确选择的开发和测试场景。
        CommandSupport.SandboxBackend sandbox = backend.equals("local")
                ? new CommandSupport.LocalBackend() : new CommandSupport.DockerBackend();
        ToolRuntime runtime = new ToolRuntime(workspace, CodingAgentCli::approve, sandbox);
        AgentLoop.ModelClient client = new AnthropicHttpClient(env.get("ANTHROPIC_API_KEY"), env.get("ANTHROPIC_BASE_URL"));
        List<AgentLoop.Message> history = new ArrayList<>();
        Scanner scanner = new Scanner(System.in);
        System.out.println("Coding Agent workspace: " + workspace + " (sandbox=" + backend + ")");
        while (true) {
            System.out.print("code >> ");
            if (!scanner.hasNextLine()) return;
            String query = scanner.nextLine().trim();
            if (query.isEmpty() || List.of("q", "quit", "exit").contains(query.toLowerCase())) return;
            runtime.resetTurn(); history.add(new AgentLoop.Message("user", query));
            AgentLoop.Report report = AgentLoop.run(client, history, runtime, model, AgentLoop.Limits.defaults());
            if (!runtime.changedFiles().isEmpty()) report.verification = runtime.runVerification();
            printReport(report);
        }
    }

    /** 所有 Patch 和任意命令执行共用的 fail-closed 人工审批入口。 */
    private static boolean approve(String prompt) {
        System.out.println("\n" + prompt + "\nAllow? [y/N] ");
        return new Scanner(System.in).nextLine().trim().matches("(?i)y|yes");
    }

    /** 加载简单 KEY=VALUE 配置；.env 文件不会进入 Agent 文件工具。 */
    static void loadEnv(Path path, Map<String, String> target) throws IOException {
        if (!Files.isRegularFile(path)) return;
        for (String line : Files.readAllLines(path)) {
            String trimmed = line.trim();
            if (trimmed.isEmpty() || trimmed.startsWith("#") || !trimmed.contains("=")) continue;
            int separator = trimmed.indexOf('=');
            target.put(trimmed.substring(0, separator).trim(), trimmed.substring(separator + 1).trim().replaceAll("^[\"']|[\"']$", ""));
        }
    }

    private static Map<String, String> parseArgs(String[] args) {
        Map<String, String> options = new HashMap<>();
        for (int i = 0; i < args.length; i++) {
            if (args[i].startsWith("--") && i + 1 < args.length) options.put(args[i].substring(2), args[++i]);
        }
        return options;
    }

    /** 输出执行状态、计划、验证证据与尚未解决的问题。 */
    private static void printReport(AgentLoop.Report report) {
        if (!report.finalText.isBlank()) System.out.println("\n" + report.finalText);
        System.out.println("\n--- Final report ---");
        System.out.println("Stopped: " + report.stoppedReason);
        System.out.println("Changed files: " + (report.changedFiles.isEmpty() ? "none" : String.join(", ", report.changedFiles)));
        System.out.println("Plan (" + report.plan.mode() + ", replans=" + report.plan.replans() + "):");
        for (Planning.PlanStep step : report.plan.steps())
            System.out.println("  " + step.id() + ". [" + step.status().name().toLowerCase() + "] " + step.title());
        System.out.println("Verification: " + (report.verification.isEmpty() ? "not run" : String.join("\n", report.verification)));
        if (!report.remainingIssues.isEmpty()) System.out.println("Remaining issues: " + String.join("; ", report.remainingIssues));
        if (!report.recoverySuggestions.isEmpty()) System.out.println("Recovery suggestions: " + String.join("; ", report.recoverySuggestions));
    }
}
