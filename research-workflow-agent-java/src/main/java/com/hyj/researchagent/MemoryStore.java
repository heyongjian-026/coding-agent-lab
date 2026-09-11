package com.hyj.researchagent;

import com.fasterxml.jackson.core.type.TypeReference;

import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashMap;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Set;
import java.util.regex.Pattern;

/** 工作、短期、长期三级 JSON 记忆，并在长期写入前执行可信来源门禁。 */
public final class MemoryStore {
    public record WorkingMemory(String taskId, List<String> plan, List<Map<String, Object>> evidence,
                                Map<String, String> progress, String updatedAt) {}
    public record ShortTermSession(String sessionId, List<Map<String, String>> messages,
                                   String summary, String updatedAt) {}
    public record LongTermMemory(String id, String category, String content, String source,
                                 double confidence, boolean confirmed, String createdAt,
                                 String updatedAt, List<String> supersedes) {}

    private static final Set<String> TRUSTED = Set.of("user", "reviewer", "lead", "migration");
    private static final Pattern TOKEN = Pattern.compile("[a-z0-9_]+|[\\p{IsHan}]+");
    private final Path workingPath, shortPath, longPath, legacyPath;
    private final int maxLongTerm, maxSessionMessages;

    public MemoryStore(Path dataDir) { this(dataDir, 200, 40); }
    public MemoryStore(Path dataDir, int maxLongTerm, int maxSessionMessages) {
        this.workingPath = dataDir.resolve("working_memory.json");
        this.shortPath = dataDir.resolve("short_term_memory.json");
        this.longPath = dataDir.resolve("long_term_memory.json");
        this.legacyPath = dataDir.resolve("memory.json");
        this.maxLongTerm = maxLongTerm; this.maxSessionMessages = maxSessionMessages;
        migrateLegacy();
    }

    /** 保存当前任务计划、证据和进度；未提供的字段沿用已有值。 */
    public synchronized WorkingMemory saveWorking(String taskId, List<String> plan,
                                                   List<Map<String, Object>> evidence,
                                                   Map<String, String> progress) {
        Map<String, WorkingMemory> all = readWorking();
        WorkingMemory old = all.get(taskId);
        WorkingMemory value = new WorkingMemory(taskId,
                plan != null ? List.copyOf(plan) : old == null ? List.of() : old.plan(),
                evidence != null ? List.copyOf(evidence) : old == null ? List.of() : old.evidence(),
                progress != null ? Map.copyOf(progress) : old == null ? Map.of() : old.progress(), JsonFiles.now());
        all.put(taskId, value); JsonFiles.write(workingPath, all); return value;
    }

    public WorkingMemory loadWorking(String taskId) { return readWorking().get(taskId); }

    public synchronized WorkingMemory addEvidence(String taskId, String content, String source, double confidence) {
        WorkingMemory old = loadWorking(taskId);
        List<Map<String, Object>> evidence = new ArrayList<>(old == null ? List.of() : old.evidence());
        evidence.add(Map.of("content", content, "source", source, "confidence", confidence(confidence), "created_at", JsonFiles.now()));
        return saveWorking(taskId, old == null ? List.of() : old.plan(), evidence, old == null ? Map.of() : old.progress());
    }

    public synchronized WorkingMemory updateProgress(String taskId, String step, String status) {
        WorkingMemory old = loadWorking(taskId);
        Map<String, String> progress = new LinkedHashMap<>(old == null ? Map.of() : old.progress());
        progress.put(step, status);
        return saveWorking(taskId, old == null ? List.of() : old.plan(), old == null ? List.of() : old.evidence(), progress);
    }

    public synchronized void clearWorking(String taskId) {
        Map<String, WorkingMemory> all = readWorking(); all.remove(taskId); JsonFiles.write(workingPath, all);
    }

    /** 追加一个会话 turn，超出上限的旧消息合并进摘要。 */
    public synchronized ShortTermSession appendTurn(String sessionId, String role, String content) {
        Map<String, ShortTermSession> sessions = readSessions();
        ShortTermSession old = sessions.getOrDefault(sessionId, new ShortTermSession(sessionId, List.of(), "", JsonFiles.now()));
        List<Map<String, String>> messages = new ArrayList<>(old.messages());
        messages.add(Map.of("role", role, "content", content, "created_at", JsonFiles.now()));
        String summary = old.summary();
        if (messages.size() > maxSessionMessages) {
            summary = mergeSummary(summary, new ArrayList<>(messages.subList(0, messages.size() - maxSessionMessages)));
            messages = new ArrayList<>(messages.subList(messages.size() - maxSessionMessages, messages.size()));
        }
        ShortTermSession value = new ShortTermSession(sessionId, List.copyOf(messages), summary, JsonFiles.now());
        sessions.put(sessionId, value); JsonFiles.write(shortPath, sessions); return value;
    }

    public ShortTermSession loadSession(String sessionId) {
        return readSessions().getOrDefault(sessionId, new ShortTermSession(sessionId, List.of(), "", JsonFiles.now()));
    }

    public synchronized void forgetSession(String sessionId) {
        Map<String, ShortTermSession> all = readSessions(); all.remove(sessionId); JsonFiles.write(shortPath, all);
    }

    /** 压缩 Agent Loop 消息，保留最近消息并把更早内容写入短期摘要。 */
    public synchronized List<Map<String, Object>> compressMessages(List<Map<String, Object>> messages,
                                                                    String sessionId, int maxMessages, int keepRecent) {
        if (messages.size() <= maxMessages) return List.copyOf(messages);
        List<Map<String, ?>> old = new ArrayList<>(messages.subList(0, messages.size() - keepRecent));
        ShortTermSession session = loadSession(sessionId);
        String summary = mergeSummary(session.summary(), old);
        Map<String, ShortTermSession> sessions = readSessions();
        sessions.put(sessionId, new ShortTermSession(sessionId, session.messages(), summary, JsonFiles.now()));
        JsonFiles.write(shortPath, sessions);
        List<Map<String, Object>> result = new ArrayList<>();
        result.add(Map.of("role", "user", "content", "[Earlier conversation summary]\n" + summary));
        result.addAll(messages.subList(messages.size() - keepRecent, messages.size()));
        return result;
    }

    /** 只允许稳定、已确认且来源可信或已经 Reviewer 审核的信息进入长期记忆。 */
    public synchronized String remember(String content, String category, String source, double confidence,
                                        boolean confirmed, String reviewedBy, boolean stable) {
        if (!stable || !confirmed || (!TRUSTED.contains(source) && (reviewedBy == null || reviewedBy.isBlank())))
            throw new IllegalArgumentException("Long-term memory requires confirmation from user, Lead, or Reviewer");
        String now = JsonFiles.now(), id = JsonFiles.id("mem");
        List<LongTermMemory> items = readLong();
        items.add(new LongTermMemory(id, category, content, source, confidence(confidence), true, now, now, List.of()));
        JsonFiles.write(longPath, evict(items)); return id;
    }

    public String remember(String content, String category) {
        return remember(content, category, "user", 1.0, true, null, true);
    }

    /** 以中英文 token 重叠度检索已确认长期记忆；空查询返回最近项目。 */
    public List<LongTermMemory> recall(String query, int limit) {
        List<LongTermMemory> items = readLong().stream().filter(LongTermMemory::confirmed).toList();
        if (query == null || query.isBlank()) return items.subList(Math.max(0, items.size() - limit), items.size());
        Set<String> wanted = tokens(query);
        return items.stream().map(item -> Map.entry(item, overlap(wanted, tokens(item.content()))))
                .filter(entry -> entry.getValue() > 0)
                .sorted(Map.Entry.<LongTermMemory, Integer>comparingByValue().reversed())
                .limit(limit).map(Map.Entry::getKey).toList();
    }

    public List<LongTermMemory> recall(String query) { return recall(query, 10); }

    /** 用新记忆替换旧记忆，并通过 supersedes 保存纠错链。 */
    public synchronized String updateMemory(String oldId, String content, String source, double confidence, String reviewedBy) {
        LongTermMemory old = readLong().stream().filter(item -> item.id().equals(oldId)).findFirst()
                .orElseThrow(() -> new IllegalArgumentException("Unknown memory: " + oldId));
        String id = remember(content, old.category(), source, confidence, true, reviewedBy, true);
        List<LongTermMemory> updated = new ArrayList<>();
        for (LongTermMemory item : readLong()) {
            if (item.id().equals(oldId)) continue;
            updated.add(item.id().equals(id) ? new LongTermMemory(item.id(), item.category(), item.content(), item.source(),
                    item.confidence(), item.confirmed(), item.createdAt(), item.updatedAt(), List.of(oldId)) : item);
        }
        JsonFiles.write(longPath, updated); return id;
    }

    public synchronized boolean forget(String id) {
        List<LongTermMemory> items = readLong();
        List<LongTermMemory> kept = items.stream().filter(item -> !item.id().equals(id)).toList();
        JsonFiles.write(longPath, kept); return kept.size() != items.size();
    }

    public List<LongTermMemory> detectConflicts(String content, String category) {
        Set<String> wanted = tokens(content);
        return readLong().stream().filter(item -> item.category().equals(category))
                .filter(item -> overlap(wanted, tokens(item.content())) > 0)
                .filter(item -> !item.content().trim().equalsIgnoreCase(content.trim())).toList();
    }

    public synchronized String merge(List<String> ids, String content, String source, double confidence) {
        List<LongTermMemory> selected = readLong().stream().filter(item -> ids.contains(item.id())).toList();
        if (selected.size() != new HashSet<>(ids).size()) throw new IllegalArgumentException("One or more memories do not exist");
        if (selected.stream().map(LongTermMemory::category).distinct().count() != 1)
            throw new IllegalArgumentException("Only memories in the same category can be merged");
        String id = remember(content, selected.get(0).category(), source, confidence, true, source, true);
        List<LongTermMemory> result = new ArrayList<>();
        for (LongTermMemory item : readLong()) {
            if (ids.contains(item.id())) continue;
            result.add(item.id().equals(id) ? new LongTermMemory(item.id(), item.category(), item.content(), item.source(),
                    item.confidence(), true, item.createdAt(), item.updatedAt(), List.copyOf(ids)) : item);
        }
        JsonFiles.write(longPath, result); return id;
    }

    /** 构造每轮模型调用所需的短期摘要和按问题召回的长期记忆。 */
    public String buildContext(String query, String sessionId, int limit) {
        return JsonFiles.toJson(Map.of("conversation_summary", loadSession(sessionId).summary(),
                "recalled_long_term_memory", recall(query, limit)));
    }

    private void migrateLegacy() {
        if (Files.exists(longPath) || !Files.exists(legacyPath)) return;
        List<Map<String, Object>> legacy = JsonFiles.read(legacyPath, new TypeReference<>() {}, List.of());
        List<LongTermMemory> result = legacy.stream().map(item -> {
            String time = String.valueOf(item.getOrDefault("created_at", JsonFiles.now()));
            return new LongTermMemory(String.valueOf(item.getOrDefault("id", JsonFiles.id("mem"))),
                    String.valueOf(item.getOrDefault("category", "fact")), String.valueOf(item.getOrDefault("content", "")),
                    "migration", 1.0, true, time, time, List.of());
        }).toList();
        JsonFiles.write(longPath, result);
    }

    private List<LongTermMemory> evict(List<LongTermMemory> items) {
        if (items.size() <= maxLongTerm) return items;
        return items.stream().sorted(Comparator.comparingDouble(LongTermMemory::confidence)
                .thenComparing(LongTermMemory::updatedAt)).skip(items.size() - maxLongTerm).toList();
    }

    private Map<String, WorkingMemory> readWorking() {
        return JsonFiles.read(workingPath, new TypeReference<>() {}, new LinkedHashMap<>());
    }
    private Map<String, ShortTermSession> readSessions() {
        return JsonFiles.read(shortPath, new TypeReference<>() {}, new LinkedHashMap<>());
    }
    private List<LongTermMemory> readLong() {
        return new ArrayList<>(JsonFiles.read(longPath, new TypeReference<>() {}, new ArrayList<>()));
    }

    private static String mergeSummary(String existing, List<? extends Map<String, ?>> messages) {
        StringBuilder value = new StringBuilder(existing == null ? "" : existing);
        for (Map<String, ?> message : messages) {
            Object role = message.containsKey("role") ? message.get("role") : "unknown";
            Object rawContent = message.containsKey("content") ? message.get("content") : "";
            String content = String.valueOf(rawContent);
            value.append(value.length() == 0 ? "" : "\n").append(role).append(": ")
                    .append(content, 0, Math.min(300, content.length()));
        }
        return value.length() <= 4000 ? value.toString() : value.substring(value.length() - 4000);
    }

    private static Set<String> tokens(String text) {
        Set<String> result = new HashSet<>(); var matcher = TOKEN.matcher(text.toLowerCase(Locale.ROOT));
        while (matcher.find()) {
            String part = matcher.group(); result.add(part);
            if (part.codePoints().allMatch(cp -> Character.UnicodeScript.of(cp) == Character.UnicodeScript.HAN)) {
                part.codePoints().forEach(cp -> result.add(new String(Character.toChars(cp))));
                for (int i = 0; i + 1 < part.length(); i++) result.add(part.substring(i, i + 2));
            }
        }
        return result;
    }
    private static int overlap(Set<String> a, Set<String> b) { Set<String> copy = new HashSet<>(a); copy.retainAll(b); return copy.size(); }
    private static double confidence(double value) {
        if (value < 0 || value > 1) throw new IllegalArgumentException("confidence must be between 0 and 1"); return value;
    }
}
