package com.hyj.codingagent;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Path;
import java.security.MessageDigest;
import java.time.Duration;
import java.util.ArrayList;
import java.util.HexFormat;
import java.util.List;
import java.util.Locale;
import java.util.Set;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.TimeUnit;
import java.util.regex.Pattern;

/**
 * 命令执行相关的协议与安全组件集合。
 *
 * <p>这里刻意把“模型提出命令”和“系统允许执行命令”分开：模型只能构造
 * {@link CommandRequest}，最终授权由确定性的 {@link CommandPolicy} 和用户审批完成。</p>
 */
public final class CommandSupport {
    private CommandSupport() {}

    /** 使用 argv 而不是 shell 字符串描述一次命令，避免 shell 运算符和二次解析。 */
    public record CommandRequest(List<String> argv, int timeoutSeconds) {
        public CommandRequest {
            argv = argv == null ? List.of() : List.copyOf(argv);
        }

        /** 校验协议边界，防止空命令以及无限制运行。 */
        public void validate() {
            if (argv.isEmpty() || argv.stream().anyMatch(value -> value == null || value.isBlank())) {
                throw new IllegalArgumentException("argv must be a non-empty array of non-empty strings");
            }
            if (timeoutSeconds < 1 || timeoutSeconds > 600) {
                throw new IllegalArgumentException("timeout must be between 1 and 600 seconds");
            }
        }

        /**
         * 为命令参数和超时生成稳定指纹。
         * 指纹用于确认用户批准的内容与真正执行的内容完全相同。
         */
        public String fingerprint() {
            validate();
            return sha256(String.join("\u0000", argv) + "\u0000" + timeoutSeconds);
        }
    }

    /** 子进程的结构化执行结果。 */
    public record CommandResult(String command, int exitCode, String stdout, String stderr, boolean timedOut) {
        /** 转换为 Agent 和错误分类器共同消费的稳定文本协议。 */
        public String render() {
            String status = timedOut ? "timed out" : "exit " + exitCode;
            String output = String.join("\n", List.of(nullToEmpty(stdout), nullToEmpty(stderr))).trim();
            return "[" + status + "]\n" + (output.isEmpty() ? "(no output)" : output);
        }
    }

    /** 确定性命令策略的判断结果；ASK 表示必须再经人工审批。 */
    public record PolicyDecision(Action action, String reason, List<String> risks) {
        public enum Action { ASK, DENY }
    }

    /** 保存审批时的命令指纹，防止审批后参数被替换。 */
    public record ApprovalTicket(String fingerprint) {
        public static ApprovalTicket create(CommandRequest request) {
            return new ApprovalTicket(request.fingerprint());
        }

        /** 执行前复核命令；不一致时 fail closed。 */
        public void verify(CommandRequest request) {
            if (!fingerprint.equals(request.fingerprint())) {
                throw new IllegalArgumentException("Command changed after approval; refusing to execute");
            }
        }
    }

    /**
     * 不依赖 LLM 的命令策略边界。
     * 破坏性命令和工作区逃逸直接拒绝，其余任意代码执行均返回 ASK。
     */
    public static final class CommandPolicy {
        private static final List<String> DENY = List.of(
                "rm -rf /", "sudo reboot", "sudo shutdown", "shutdown", "reboot", "mkfs",
                "dd if=", "> /dev/sda", "format c:");
        private static final Pattern WINDOWS_ABSOLUTE = Pattern.compile("^[A-Za-z]:.*");
        private final Path workspace;

        public CommandPolicy(Path workspace) {
            this.workspace = workspace.toAbsolutePath().normalize();
        }

        /** 根据拒绝列表和路径边界评估命令。 */
        public PolicyDecision evaluate(CommandRequest request) {
            request.validate();
            String command = String.join(" ", request.argv()).toLowerCase(Locale.ROOT);
            if (DENY.stream().anyMatch(command::contains)) {
                return new PolicyDecision(PolicyDecision.Action.DENY,
                        "command matches the destructive deny list", List.of("destructive"));
            }
            for (String argument : request.argv().subList(1, request.argv().size())) {
                if (outsidePath(argument)) {
                    return new PolicyDecision(PolicyDecision.Action.DENY,
                            "path argument is outside workspace: " + argument, List.of("path_escape"));
                }
            }
            return new PolicyDecision(PolicyDecision.Action.ASK,
                    "arbitrary command execution requires approval", List.of("code_execution"));
        }

        /**
         * 对看起来像路径的参数做工作区包含检查。
         * URL、普通单词和选项不在这里解析，复杂系统应使用按工具定制的参数策略。
         */
        private boolean outsidePath(String argument) {
            if (argument.startsWith("-") || argument.contains("://")) return false;
            boolean pathLike = argument.startsWith(".") || argument.startsWith("/")
                    || argument.startsWith("\\") || argument.contains("/")
                    || argument.contains("\\") || WINDOWS_ABSOLUTE.matcher(argument).matches();
            if (!pathLike) return false;
            try {
                Path path = Path.of(argument);
                Path resolved = path.isAbsolute() ? path.normalize() : workspace.resolve(path).normalize();
                return !resolved.startsWith(workspace);
            } catch (RuntimeException error) {
                return true;
            }
        }
    }

    /** 命令执行后端接口，使生产 Docker 沙箱和测试后端可以替换。 */
    public interface SandboxBackend {
        CommandResult execute(CommandRequest request, Path workspace);
    }

    /**
     * 直接在宿主机启动 argv 进程的兼容后端。
     * 该实现便于测试，但不是安全边界，真实 Agent 默认应使用 DockerBackend。
     */
    public static final class LocalBackend implements SandboxBackend {
        @Override
        /** 启动无 shell 子进程，并并发读取输出以避免管道缓冲区阻塞。 */
        public CommandResult execute(CommandRequest request, Path workspace) {
            request.validate();
            Process process = null;
            try {
                ProcessBuilder builder = new ProcessBuilder(request.argv());
                builder.directory(workspace.toAbsolutePath().normalize().toFile());
                builder.redirectInput(ProcessBuilder.Redirect.from(Path.of(System.getProperty("os.name")
                        .toLowerCase(Locale.ROOT).contains("win") ? "NUL" : "/dev/null").toFile()));
                process = builder.start();
                Process active = process;
                CompletableFuture<String> stdout = CompletableFuture.supplyAsync(() -> read(active.getInputStream()));
                CompletableFuture<String> stderr = CompletableFuture.supplyAsync(() -> read(active.getErrorStream()));
                // 超时必须强制销毁进程，防止失败命令在 Agent 返回后继续运行。
                if (!process.waitFor(request.timeoutSeconds(), TimeUnit.SECONDS)) {
                    process.destroyForcibly();
                    return new CommandResult(show(request.argv()), -1, clip(stdout.join()),
                            "Timed out after " + request.timeoutSeconds() + "s", true);
                }
                return new CommandResult(show(request.argv()), process.exitValue(), clip(stdout.join()), clip(stderr.join()), false);
            } catch (Exception error) {
                if (process != null) process.destroyForcibly();
                return new CommandResult(show(request.argv()), -1, "", error.toString(), false);
            }
        }
    }

    /**
     * 一次性 Docker 沙箱后端。
     * 容器断网、根文件系统只读、移除 capabilities，只把当前工作区挂载到 /workspace。
     */
    public static final class DockerBackend implements SandboxBackend {
        private static final Pattern IMAGE = Pattern.compile("[A-Za-z0-9][A-Za-z0-9._/:@-]*");
        private final String image;
        private final String dockerCli;

        public DockerBackend() {
            this("eclipse-temurin:17-jdk", System.getenv().getOrDefault("DOCKER_CLI", "docker"));
        }

        public DockerBackend(String image, String dockerCli) {
            if (!IMAGE.matcher(image).matches()) throw new IllegalArgumentException("Invalid Docker image name");
            this.image = image;
            this.dockerCli = dockerCli;
        }

        /** 构造 Docker CLI argv；单独暴露此方法便于不启动 Docker 就测试隔离参数。 */
        public List<String> buildCommand(CommandRequest request, Path workspace) {
            request.validate();
            List<String> result = new ArrayList<>(List.of(dockerCli, "run", "--rm", "--network", "none",
                    "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                    "--memory", "512m", "--cpus", "1", "--pids-limit", "128",
                    "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m", "--mount",
                    "type=bind,source=" + workspace.toAbsolutePath().normalize() + ",target=/workspace",
                    "--workdir", "/workspace", image));
            result.addAll(request.argv());
            return result;
        }

        @Override
        /** 在宿主机启动 Docker CLI，由容器执行真正的用户命令。 */
        public CommandResult execute(CommandRequest request, Path workspace) {
            return new LocalBackend().execute(new CommandRequest(buildCommand(request, workspace), request.timeoutSeconds()), workspace);
        }
    }

    /** 把工具文本结果归一化为熔断和恢复逻辑使用的错误类别。 */
    public static String classifyFailure(String output) {
        String value = output == null ? "" : output.toLowerCase(Locale.ROOT);
        if (value.startsWith("denied")) return "permission";
        if (value.contains("timed out")) return "timeout";
        if (value.contains("malformed tool arguments") || value.startsWith("error: keyerror")) return "invalid_arguments";
        if (value.startsWith("error:") || value.startsWith("error[") || value.startsWith("[exit -1]")) return "permanent";
        var matcher = Pattern.compile("^\\[exit (-?\\d+)]").matcher(value);
        return matcher.find() && !matcher.group(1).equals("0") ? "command_failed" : null;
    }

    /** 返回确定性的恢复动作，建议本身不依赖也不授权 LLM。 */
    public static String recoverySuggestion(String failure) {
        return switch (failure) {
            case "permission" -> "ask_human — Request different scope or user approval.";
            case "timeout" -> "retry — Retry once with a justified timeout or a smaller command.";
            case "invalid_arguments" -> "correct_arguments — Correct the structured tool arguments.";
            case "command_failed" -> "alternative — Inspect stderr and choose a different fix or command.";
            default -> "stop — Stop this path and report the blocking error.";
        };
    }

    /** 判断命令是否能作为测试、构建或静态检查证据。 */
    public static boolean isVerificationCommand(List<String> argv) {
        String value = String.join(" ", argv).toLowerCase(Locale.ROOT);
        return Set.of("pytest", "mvn test", "mvn verify", "gradle test", "gradlew test", "npm test",
                "npm run build", " test", "build", "lint", "mypy").stream().anyMatch(value::contains);
    }

    /** 仅用于日志和审批界面展示 argv，不会把结果交回 shell 执行。 */
    public static String show(List<String> argv) {
        return argv.stream().map(value -> value.matches("[A-Za-z0-9_./:@=-]+") ? value
                : "\"" + value.replace("\"", "\\\"") + "\"").reduce((a, b) -> a + " " + b).orElse("");
    }

    /** 限制模型上下文和日志中的子进程输出体积。 */
    static String clip(String value) {
        if (value == null) return "";
        return value.length() <= 20_000 ? value : value.substring(0, 20_000) + "\n...[output truncated]";
    }

    private static String read(java.io.InputStream stream) {
        try (stream) {
            return new String(stream.readAllBytes(), StandardCharsets.UTF_8);
        } catch (IOException error) {
            return error.toString();
        }
    }

    private static String sha256(String value) {
        try {
            return HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256")
                    .digest(value.getBytes(StandardCharsets.UTF_8)));
        } catch (Exception impossible) {
            throw new IllegalStateException(impossible);
        }
    }

    private static String nullToEmpty(String value) { return value == null ? "" : value; }
}
