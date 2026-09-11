package com.hyj.researchagent;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import java.nio.file.Files;
import java.nio.file.Path;
import java.time.LocalDateTime;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.*;

class MemoryAndStoresTest {
    @TempDir Path root;

    @Test void threeMemoryTiersAreSeparateAndPersistent() {
        MemoryStore store=new MemoryStore(root);
        store.saveWorking("task-1",List.of("search","write"),List.of(),Map.of("search","pending"));
        store.addEvidence("task-1","paper result","doi:1",0.8);
        store.appendTurn("session-1","user","研究 Agent memory");
        store.remember("用户偏好中文","preference");
        MemoryStore again=new MemoryStore(root);
        assertEquals("doi:1",again.loadWorking("task-1").evidence().get(0).get("source"));
        assertEquals("user",again.loadSession("session-1").messages().get(0).get("role"));
        assertEquals(0.8,((Number)again.loadWorking("task-1").evidence().get(0).get("confidence")).doubleValue());
        assertEquals("preference",again.recall("偏好中文").get(0).category());
    }

    @Test void longTermMemoryRequiresTrustAndSupportsCorrection() {
        MemoryStore store=new MemoryStore(root);
        assertThrows(IllegalArgumentException.class,()->store.remember("unverified","fact","retrieval",0.5,false,null,true));
        String old=store.remember("model A accuracy is 80%","confirmed_conclusion");
        assertEquals(old,store.detectConflicts("model A accuracy is 82%","confirmed_conclusion").get(0).id());
        String updated=store.updateMemory(old,"model A accuracy is 82%","reviewer",1.0,"lead");
        assertEquals(List.of(old),store.recall("accuracy").get(0).supersedes());
        assertTrue(store.forget(updated));
    }

    @Test void evictionKeepsHigherConfidenceMemories() {
        MemoryStore store=new MemoryStore(root,2,40);
        store.remember("low","fact","user",0.1,true,null,true);
        store.remember("high","fact","user",0.9,true,null,true);
        store.remember("medium","fact","user",0.5,true,null,true);
        assertEquals(List.of("high","medium"),store.recall("",10).stream().map(MemoryStore.LongTermMemory::content).sorted().toList());
    }

    @Test void shortTermCompressionPersistsSummary() {
        MemoryStore store=new MemoryStore(root);
        List<Map<String,Object>> messages=new ArrayList<>();
        for(int i=0;i<15;i++)messages.add(Map.of("role","user","content","turn "+i));
        List<Map<String,Object>> compressed=store.compressMessages(messages,"resume",12,6);
        assertEquals(7,compressed.size());
        assertTrue(store.loadSession("resume").summary().contains("turn 0"));
    }

    @Test void taskDependenciesAndLifecycleArePersistent() {
        var tasks=new ResearchStores.TaskStore(root);var first=tasks.createTask("read","",List.of());
        var second=tasks.createTask("experiment","",List.of(first.id()));
        assertFalse(tasks.canStart(second.id()));assertTrue(tasks.claimTask(first.id(),"reader").contains("Claimed"));
        tasks.completeTask(first.id());assertTrue(tasks.canStart(second.id()));
    }

    @Test void notificationNeedsApprovalAndWritesOutbox() throws Exception {
        var approvals=new ResearchStores.ApprovalStore(root);var notifications=new ResearchStores.NotificationStore(root,approvals);
        var item=notifications.requestNotification("email","advisor@example.com","weekly report");
        assertTrue(notifications.deliverNotification(item.id()).startsWith("blocked"));
        approvals.reviewApproval(item.approvalId(),true);assertTrue(notifications.deliverNotification(item.id()).startsWith("Delivered"));
        assertTrue(Files.readString(root.resolve("notification_outbox.jsonl")).contains("weekly report"));
    }

    @Test void cronMatchesAndDeduplicatesMinute() {
        var cron=new ResearchStores.CronScheduler(root);cron.scheduleJob("30 9 * * 1","weekly",true);
        LocalDateTime monday=LocalDateTime.of(2026,8,10,9,30);
        assertEquals(1,cron.dueJobs(monday).size());assertTrue(cron.dueJobs(monday).isEmpty());
        assertNotNull(ResearchStores.CronScheduler.validateCron("*/0 * * * *"));
    }

    @Test void localVectorKnowledgeRetrievesRelevantDocument() throws Exception {
        var kb=new KnowledgeService.LocalKnowledgeBase(root,new KnowledgeService.LocalEmbeddingModel(64));
        kb.addDocument("Agent","agent context compression token","manual",800);
        kb.addDocument("Database","database index query","manual",800);
        assertEquals("Agent",kb.search("agent context",1).get(0).get("title"));
        assertTrue(Files.readString(root.resolve("knowledge.json")).contains("embedding"));
    }
}
