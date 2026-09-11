package com.hyj.researchagent;

import com.fasterxml.jackson.core.type.TypeReference;

import java.nio.file.Files;
import java.nio.file.Path;
import java.time.DayOfWeek;
import java.time.LocalDateTime;
import java.time.format.DateTimeFormatter;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/** 科研任务、人工审批、通知 Outbox 和轻量 Cron 的文件存储组件。 */
public final class ResearchStores {
    private ResearchStores() {}

    public record Task(String id, String subject, String description, String status,
                       String owner, List<String> blockedBy, String createdAt, String updatedAt) {}

    /** 持久化任务依赖和生命周期。 */
    public static final class TaskStore {
        private final Path path;
        public TaskStore(Path dataDir) { path = dataDir.resolve("tasks.json"); }

        public synchronized Task createTask(String subject, String description, List<String> blockedBy) {
            String now = JsonFiles.now();
            Task task = new Task(JsonFiles.id("task"), subject, description, "pending", "",
                    blockedBy == null ? List.of() : List.copyOf(blockedBy), now, now);
            Map<String, Task> all = read(); all.put(task.id(), task); JsonFiles.write(path, all); return task;
        }

        public Task loadTask(String id) {
            Task task = read().get(id); if (task == null) throw new IllegalArgumentException("Unknown task: " + id); return task;
        }
        public List<Task> listTasks() { return new ArrayList<>(read().values()); }
        public boolean canStart(String id) {
            Task task = loadTask(id); Map<String, Task> all = read();
            return task.blockedBy().stream().allMatch(dep -> all.containsKey(dep) && all.get(dep).status().equals("completed"));
        }
        public synchronized String claimTask(String id, String owner) {
            if (!canStart(id)) return "Blocked by unfinished dependencies";
            Task old = loadTask(id); Task next = copy(old, "in_progress", owner); save(next); return "Claimed " + id;
        }
        public synchronized String completeTask(String id) {
            Task old = loadTask(id); Task next = copy(old, "completed", old.owner()); save(next); return "Completed " + id;
        }
        private void save(Task task) { Map<String, Task> all = read(); all.put(task.id(), task); JsonFiles.write(path, all); }
        private Task copy(Task old, String status, String owner) {
            return new Task(old.id(), old.subject(), old.description(), status, owner, old.blockedBy(), old.createdAt(), JsonFiles.now());
        }
        private Map<String, Task> read() { return JsonFiles.read(path, new TypeReference<>() {}, new LinkedHashMap<>()); }
    }

    public record ApprovalRequest(String id, String action, Map<String, Object> payload,
                                  String status, String createdAt, String reviewedAt) {}

    /** 所有外发或有副作用动作共享的人工审批状态机。 */
    public static final class ApprovalStore {
        private final Path path;
        public ApprovalStore(Path dataDir) { path = dataDir.resolve("approvals.json"); }
        public synchronized ApprovalRequest requestApproval(String action, Map<String, Object> payload) {
            ApprovalRequest request = new ApprovalRequest(JsonFiles.id("approval"), action, Map.copyOf(payload),
                    "pending", JsonFiles.now(), "");
            Map<String, ApprovalRequest> all = read(); all.put(request.id(), request); JsonFiles.write(path, all); return request;
        }
        public synchronized String reviewApproval(String id, boolean approve) {
            ApprovalRequest old = getApproval(id);
            if (old == null) throw new IllegalArgumentException("Unknown approval: " + id);
            ApprovalRequest next = new ApprovalRequest(old.id(), old.action(), old.payload(), approve ? "approved" : "rejected",
                    old.createdAt(), JsonFiles.now());
            Map<String, ApprovalRequest> all = read(); all.put(id, next); JsonFiles.write(path, all); return next.status();
        }
        public synchronized void markExecuted(String id) {
            ApprovalRequest old = getApproval(id);
            if (old == null || !old.status().equals("approved")) throw new IllegalStateException("Approval is not approved");
            Map<String, ApprovalRequest> all = read();
            all.put(id, new ApprovalRequest(old.id(), old.action(), old.payload(), "executed", old.createdAt(), old.reviewedAt()));
            JsonFiles.write(path, all);
        }
        public ApprovalRequest getApproval(String id) { return read().get(id); }
        public List<ApprovalRequest> listApprovals() { return new ArrayList<>(read().values()); }
        public boolean approved(String id) { ApprovalRequest item = getApproval(id); return item != null && item.status().equals("approved"); }
        private Map<String, ApprovalRequest> read() { return JsonFiles.read(path, new TypeReference<>() {}, new LinkedHashMap<>()); }
    }

    public record Notification(String id, String channel, String recipient, String content,
                               String status, String approvalId, String createdAt, String deliveredAt) {}

    /** 经审批后只写入本地 Outbox 的通知组件，不冒充真实邮件发送。 */
    public static final class NotificationStore {
        private final Path path, outbox; private final ApprovalStore approvals;
        public NotificationStore(Path dataDir, ApprovalStore approvals) {
            this.path = dataDir.resolve("notifications.json"); this.outbox = dataDir.resolve("notification_outbox.jsonl"); this.approvals = approvals;
        }
        public synchronized Notification requestNotification(String channel, String recipient, String content) {
            ApprovalRequest approval = approvals.requestApproval("send_notification",
                    Map.of("channel", channel, "recipient", recipient));
            Notification item = new Notification(JsonFiles.id("notification"), channel, recipient, content,
                    "pending", approval.id(), JsonFiles.now(), "");
            Map<String, Notification> all = read(); all.put(item.id(), item); JsonFiles.write(path, all); return item;
        }
        public synchronized String deliverNotification(String id) {
            Notification old = read().get(id); if (old == null) throw new IllegalArgumentException("Unknown notification: " + id);
            if (!approvals.approved(old.approvalId())) return "blocked: human approval required";
            Notification sent = new Notification(old.id(), old.channel(), old.recipient(), old.content(), "delivered",
                    old.approvalId(), old.createdAt(), JsonFiles.now());
            Map<String, Notification> all = read(); all.put(id, sent); JsonFiles.write(path, all);
            JsonFiles.appendJsonLine(outbox, sent); approvals.markExecuted(old.approvalId()); return "Delivered " + id;
        }
        public List<Notification> listNotifications() { return new ArrayList<>(read().values()); }
        private Map<String, Notification> read() { return JsonFiles.read(path, new TypeReference<>() {}, new LinkedHashMap<>()); }
    }

    public record CronJob(String id, String cron, String prompt, boolean recurring, String lastRunMinute, String createdAt) {}

    /** 五字段 Cron 的最小持久化调度器，同一分钟只返回一次任务。 */
    public static final class CronScheduler {
        private final Path path;
        public CronScheduler(Path dataDir) { path = dataDir.resolve("cron_jobs.json"); }
        public synchronized CronJob scheduleJob(String cron, String prompt, boolean recurring) {
            String error = validateCron(cron); if (error != null) throw new IllegalArgumentException(error);
            CronJob job = new CronJob(JsonFiles.id("cron"), cron, prompt, recurring, "", JsonFiles.now());
            Map<String, CronJob> all = read(); all.put(job.id(), job); JsonFiles.write(path, all); return job;
        }
        public List<CronJob> listJobs() { return new ArrayList<>(read().values()); }
        public synchronized List<CronJob> dueJobs(LocalDateTime now) {
            Map<String, CronJob> all = read(); List<CronJob> due = new ArrayList<>();
            String minute = now.format(DateTimeFormatter.ofPattern("yyyy-MM-dd'T'HH:mm"));
            for (CronJob job : all.values()) {
                if (!job.lastRunMinute().equals(minute) && cronMatches(job.cron(), now)) {
                    due.add(job); all.put(job.id(), new CronJob(job.id(), job.cron(), job.prompt(), job.recurring(), minute, job.createdAt()));
                }
            }
            JsonFiles.write(path, all); return due;
        }
        public static String validateCron(String expression) {
            String[] fields = expression.trim().split("\\s+");
            if (fields.length != 5) return "cron must have five fields";
            int[][] ranges = {{0,59},{0,23},{1,31},{1,12},{0,7}};
            for (int i=0;i<5;i++) try { validateField(fields[i], ranges[i][0], ranges[i][1]); }
            catch (IllegalArgumentException error) { return error.getMessage(); }
            return null;
        }
        public static boolean cronMatches(String expression, LocalDateTime time) {
            String[] f = expression.split("\\s+"); int day = time.getDayOfWeek() == DayOfWeek.SUNDAY ? 0 : time.getDayOfWeek().getValue();
            return fieldMatches(f[0], time.getMinute(), 0, 59) && fieldMatches(f[1], time.getHour(), 0, 23)
                    && fieldMatches(f[2], time.getDayOfMonth(), 1, 31) && fieldMatches(f[3], time.getMonthValue(), 1, 12)
                    && fieldMatches(f[4], day, 0, 7);
        }
        private static void validateField(String field, int min, int max) {
            if (field.equals("*")) return;
            if (field.startsWith("*/")) { int step = Integer.parseInt(field.substring(2)); if (step <= 0) throw new IllegalArgumentException("cron step must be positive"); return; }
            int value = Integer.parseInt(field); if (value < min || value > max) throw new IllegalArgumentException("cron value out of range");
        }
        private static boolean fieldMatches(String field, int value, int min, int max) {
            validateField(field, min, max); if (field.equals("*")) return true;
            if (field.startsWith("*/")) return value % Integer.parseInt(field.substring(2)) == 0;
            int expected = Integer.parseInt(field); return max == 7 && expected == 7 ? value == 0 : value == expected;
        }
        private Map<String, CronJob> read() { return JsonFiles.read(path, new TypeReference<>() {}, new LinkedHashMap<>()); }
    }
}
