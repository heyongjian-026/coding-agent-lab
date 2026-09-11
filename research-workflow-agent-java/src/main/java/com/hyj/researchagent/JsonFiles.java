package com.hyj.researchagent;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.SerializationFeature;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;
import java.nio.file.StandardOpenOption;
import java.time.Instant;
import java.util.Map;
import java.util.UUID;

/** 线程安全 JSON 持久化、JSONL 追加和 ID/时间工具。 */
final class JsonFiles {
    static final ObjectMapper JSON = new ObjectMapper().enable(SerializationFeature.INDENT_OUTPUT);
    private JsonFiles() {}

    static String id(String prefix) { return prefix + "_" + UUID.randomUUID().toString().replace("-", ""); }
    static String now() { return Instant.now().toString(); }

    static synchronized <T> T read(Path path, TypeReference<T> type, T fallback) {
        if (!Files.exists(path)) return fallback;
        try { return JSON.readValue(path.toFile(), type); }
        catch (IOException error) { throw new IllegalStateException("Cannot read " + path, error); }
    }

    static synchronized void write(Path path, Object value) {
        try {
            Files.createDirectories(path.toAbsolutePath().getParent());
            Path temp = Files.createTempFile(path.toAbsolutePath().getParent(), ".research-agent-", ".tmp");
            JSON.writeValue(temp.toFile(), value);
            try { Files.move(temp, path, StandardCopyOption.REPLACE_EXISTING, StandardCopyOption.ATOMIC_MOVE); }
            catch (java.nio.file.AtomicMoveNotSupportedException ignored) {
                Files.move(temp, path, StandardCopyOption.REPLACE_EXISTING);
            }
        } catch (IOException error) { throw new IllegalStateException("Cannot write " + path, error); }
    }

    static synchronized void appendJsonLine(Path path, Object value) {
        try {
            Files.createDirectories(path.toAbsolutePath().getParent());
            // JSONL 每条记录必须严格占一行，不能继承用于普通 JSON 文件的缩进配置。
            String line = JSON.writer().without(SerializationFeature.INDENT_OUTPUT).writeValueAsString(value);
            Files.writeString(path, line + "\n", StandardCharsets.UTF_8,
                    StandardOpenOption.CREATE, StandardOpenOption.APPEND);
        } catch (IOException error) { throw new IllegalStateException("Cannot append " + path, error); }
    }

    static Map<String, Object> map(Object value) {
        return JSON.convertValue(value, new TypeReference<>() {});
    }

    static String toJson(Object value) {
        try { return JSON.writeValueAsString(value); }
        catch (IOException error) { throw new IllegalStateException("Cannot serialize JSON", error); }
    }
}
