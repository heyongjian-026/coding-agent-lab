package com.hyj.codingagent;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.FileSystems;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;
import java.security.MessageDigest;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.HexFormat;
import java.util.List;
import java.util.Locale;
import java.util.Set;
import java.util.regex.Pattern;

/**
 * 工作区文件工具及其安全边界。
 *
 * <p>所有模型提供的路径必须先经过 {@link #resolve(Path, String)}，文件修改则采用
 * “预览并记录哈希—用户审批—复核哈希—原子替换”的流程。</p>
 */
public final class WorkspaceTools {
    private static final Set<String> SENSITIVE = Set.of(".env", ".env.local", ".env.production");
    private static final List<Pattern> INJECTION_PATTERNS = List.of(
            Pattern.compile("ignore (?:all )?(?:previous|prior) instructions", Pattern.CASE_INSENSITIVE),
            Pattern.compile("override (?:the )?(?:system|developer) prompt", Pattern.CASE_INSENSITIVE),
            Pattern.compile("reveal (?:the )?(?:system prompt|api key|secret)", Pattern.CASE_INSENSITIVE),
            Pattern.compile("disable (?:the )?(?:sandbox|safety|approval)", Pattern.CASE_INSENSITIVE));

    private WorkspaceTools() {}

    /** Patch 审批阶段冻结的目标、前后内容、差异和原文指纹。 */
    public record PatchPreview(Path target, String original, String updated, String diff, String originalHash) {}

    /** 解析模型提供的路径，并拒绝工作区外路径及已有符号链接逃逸。 */
    public static Path resolve(Path workspace, String value) {
        Path root = workspace.toAbsolutePath().normalize();
        Path supplied = Path.of(value);
        Path candidate = supplied.isAbsolute() ? supplied.normalize() : root.resolve(supplied).normalize();
        try {
            if (Files.exists(candidate)) candidate = candidate.toRealPath();
        } catch (IOException error) {
            throw new IllegalArgumentException("Cannot resolve path: " + value, error);
        }
        if (!candidate.startsWith(root)) throw new IllegalArgumentException("Path is outside workspace: " + value);
        return candidate;
    }

    /** 按行读取文件片段，避免一次把超大文件全部放入模型上下文。 */
    public static String readFile(Path workspace, String path, int offset, int limit) throws IOException {
        Path target = resolve(workspace, path);
        rejectSensitive(target);
        if (offset < 0 || limit <= 0) throw new IllegalArgumentException("offset must be >= 0 and limit must be > 0");
        if (!Files.isRegularFile(target)) throw new IllegalArgumentException("Not a file: " + path);
        List<String> lines = Files.readAllLines(target, StandardCharsets.UTF_8);
        int start = Math.min(offset, lines.size());
        int end = Math.min(start + limit, lines.size());
        return String.join("\n", lines.subList(start, end));
    }

    /** 在工作区内执行 glob，并返回排序后的相对文件路径。 */
    public static String glob(Path workspace, String pattern) throws IOException {
        Path root = workspace.toAbsolutePath().normalize();
        var matcher = FileSystems.getDefault().getPathMatcher("glob:" + pattern);
        try (var paths = Files.walk(root)) {
            List<String> matches = paths.filter(Files::isRegularFile)
                    .filter(path -> matcher.matches(root.relativize(path)))
                    .map(path -> root.relativize(path).toString().replace('\\', '/'))
                    .sorted().toList();
            return matches.isEmpty() ? "(no matches)" : String.join("\n", matches);
        }
    }

    /**
     * 计算但不写入 Patch；旧文本必须恰好出现一次，空旧文本只允许创建文件。
     */
    public static PatchPreview previewPatch(Path workspace, String path, String oldText, String newText) throws IOException {
        Path target = resolve(workspace, path);
        rejectSensitive(target);
        String original = Files.exists(target) ? Files.readString(target, StandardCharsets.UTF_8) : "";
        if (!Files.exists(target) && !oldText.isEmpty()) {
            throw new IllegalArgumentException("Cannot replace text in a file that does not exist");
        }
        if (Files.exists(target) && oldText.isEmpty()) {
            throw new IllegalArgumentException("old_text may be empty only when creating a new file");
        }
        if (!oldText.isEmpty() && count(original, oldText) != 1) {
            throw new IllegalArgumentException("old_text must occur exactly once");
        }
        String updated = oldText.isEmpty() ? newText : original.replace(oldText, newText);
        String relative = workspace.toAbsolutePath().normalize().relativize(target).toString().replace('\\', '/');
        return new PatchPreview(target, original, updated, simpleDiff(relative, original, updated), hash(original));
    }

    /**
     * 执行已经批准的 Patch。写入前重新计算原文哈希，避免 TOCTOU 导致批准内容失效。
     */
    public static String applyPatch(Path workspace, String path, PatchPreview preview) throws IOException {
        Path target = resolve(workspace, path);
        String current = Files.exists(target) ? Files.readString(target, StandardCharsets.UTF_8) : "";
        if (!hash(current).equals(preview.originalHash())) {
            throw new IllegalArgumentException("File changed after patch approval; refusing to write");
        }
        if (current.equals(preview.updated())) return "No changes to " + path;
        if (target.getParent() != null) Files.createDirectories(target.getParent());
        // 先写同目录临时文件，再替换目标，避免进程中断留下半个文件。
        Path temp = Files.createTempFile(target.getParent(), ".coding-agent-", ".tmp");
        Files.writeString(temp, preview.updated(), StandardCharsets.UTF_8);
        try {
            Files.move(temp, target, StandardCopyOption.REPLACE_EXISTING, StandardCopyOption.ATOMIC_MOVE);
        } catch (java.nio.file.AtomicMoveNotSupportedException ignored) {
            Files.move(temp, target, StandardCopyOption.REPLACE_EXISTING);
        }
        return "Updated " + workspace.toAbsolutePath().normalize().relativize(target).toString().replace('\\', '/');
    }

    /** 对不可信文件做提示词注入特征告警；结果只告警，不代替权限策略。 */
    public static List<String> detectPromptInjection(String text) {
        return INJECTION_PATTERNS.stream().filter(pattern -> pattern.matcher(text).find())
                .map(Pattern::pattern).toList();
    }

    /** 根据项目标志文件发现最小测试命令，返回结构化 argv。 */
    public static List<List<String>> verificationCommands(Path workspace) {
        List<List<String>> commands = new ArrayList<>();
        if (Files.exists(workspace.resolve("pom.xml"))) commands.add(List.of("mvn", "test", "-q"));
        else if (Files.exists(workspace.resolve("gradlew"))) commands.add(List.of("./gradlew", "test"));
        if (Files.exists(workspace.resolve("pytest.ini")) || Files.exists(workspace.resolve("pyproject.toml"))
                || Files.isDirectory(workspace.resolve("tests"))) commands.add(List.of("python", "-m", "pytest", "-q"));
        if (Files.exists(workspace.resolve("package.json"))) commands.add(List.of("npm", "test", "--", "--runInBand"));
        return commands;
    }

    /** 拒绝 Agent 读取或修改常见凭据文件。 */
    private static void rejectSensitive(Path target) {
        String name = target.getFileName().toString().toLowerCase(Locale.ROOT);
        if (SENSITIVE.contains(name) || name.startsWith(".env.")) {
            throw new IllegalArgumentException("Refusing to access sensitive file: " + name);
        }
    }

    private static int count(String source, String needle) {
        int result = 0;
        for (int index = 0; (index = source.indexOf(needle, index)) >= 0; index += needle.length()) result++;
        return result;
    }

    private static String simpleDiff(String path, String before, String after) {
        if (before.equals(after)) return "(no changes)";
        StringBuilder diff = new StringBuilder("--- ").append(path).append("\n+++ ").append(path).append('\n');
        if (!before.isEmpty()) diff.append("-").append(before.replace("\n", "\n-")).append('\n');
        if (!after.isEmpty()) diff.append("+").append(after.replace("\n", "\n+")).append('\n');
        return diff.toString();
    }

    private static String hash(String value) {
        try {
            return HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256")
                    .digest(value.getBytes(StandardCharsets.UTF_8)));
        } catch (Exception impossible) {
            throw new IllegalStateException(impossible);
        }
    }
}
