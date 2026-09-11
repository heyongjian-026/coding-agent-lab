package com.hyj.codingagent;

import com.fasterxml.jackson.databind.ObjectMapper;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardOpenOption;
import java.time.LocalDateTime;
import java.time.format.DateTimeFormatter;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.regex.Pattern;

/**
 * 每个 Agent 会话一个 JSONL 文件的本地审计记录器。
 * 日志在序列化前省略源码/Patch 正文、截断长输出，序列化后再统一脱敏。
 */
public final class SessionLogger {
    private static final ObjectMapper JSON = new ObjectMapper();
    private static final Pattern FIELD_SECRET = Pattern.compile(
            "(?i)(api[_-]?key|token|password|secret)(\\s*[:=]\\s*)[^\\s,}\\\"]+");
    private static final Pattern BEARER = Pattern.compile("(?i)bearer\\s+[a-z0-9._-]+");
    private static final Pattern KEY = Pattern.compile("\\b(?:sk|pk)-[A-Za-z0-9_-]{12,}\\b");
    private final Path path;

    /** 使用时间戳生成会话 ID。 */
    public SessionLogger(Path workspace) { this(workspace, null); }

    /** 使用可选固定 ID 创建日志，固定 ID 主要用于测试。 */
    public SessionLogger(Path workspace, String sessionId) {
        try {
            Path directory = workspace.toAbsolutePath().normalize().resolve(".coding-agent");
            Files.createDirectories(directory);
            String id = sessionId == null ? LocalDateTime.now().format(DateTimeFormatter.ofPattern("yyyyMMdd-HHmmss-SSS")) : sessionId;
            path = directory.resolve("session-" + id + ".jsonl");
        } catch (IOException error) {
            throw new IllegalStateException("Cannot create session log", error);
        }
    }

    public Path path() { return path; }

    /** 线程安全地追加一条事件；不保存完整 Patch 和超长工具输出。 */
    public synchronized void log(String event, Map<String, ?> data) {
        try {
            Map<String, Object> record = new LinkedHashMap<>();
            record.put("time", LocalDateTime.now().withNano(0).toString());
            record.put("event", event);
            record.put("data", redact(JSON.writeValueAsString(safe(data))));
            Files.writeString(path, JSON.writeValueAsString(record) + "\n", StandardCharsets.UTF_8,
                    StandardOpenOption.CREATE, StandardOpenOption.APPEND);
        } catch (IOException error) {
            throw new IllegalStateException("Cannot write session log", error);
        }
    }

    /** 在序列化前删除大段源码并限制输出长度。 */
    private static Map<String, Object> safe(Map<String, ?> input) {
        Map<String, Object> result = new LinkedHashMap<>(input);
        Object rawArgs = result.get("args");
        if (rawArgs instanceof Map<?, ?> args) {
            Map<String, Object> safeArgs = new LinkedHashMap<>();
            args.forEach((key, value) -> {
                String name = String.valueOf(key);
                safeArgs.put(name, switch (name) {
                    case "old_text", "new_text", "content" -> "[OMITTED " + String.valueOf(value).length() + " chars]";
                    default -> value;
                });
            });
            result.put("args", safeArgs);
        }
        if (result.containsKey("output")) {
            String output = String.valueOf(result.get("output"));
            result.put("output", output.length() <= 1000 ? output : output.substring(0, 1000) + "...[truncated in log]");
        }
        return result;
    }

    /** 对敏感字段、Bearer Token 和常见密钥格式做最终兜底脱敏。 */
    static String redact(String text) {
        String value = FIELD_SECRET.matcher(text).replaceAll("$1$2[REDACTED]");
        value = BEARER.matcher(value).replaceAll("Bearer [REDACTED]");
        return KEY.matcher(value).replaceAll("[REDACTED_KEY]");
    }
}
