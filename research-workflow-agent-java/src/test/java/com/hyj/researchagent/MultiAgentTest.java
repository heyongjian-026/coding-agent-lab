package com.hyj.researchagent;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import java.nio.file.Path;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;

import static org.junit.jupiter.api.Assertions.*;

class MultiAgentTest {
    @TempDir Path root;

    private String payload(String role){return JsonFiles.toJson(switch(role){
        case "planner"->Map.of("plan",List.of("search","write"),"success_criteria",List.of("cited"));
        case "researcher"->Map.of("evidence",List.of(Map.of("content","finding","source","doi:1","confidence",0.8)),"confidence",0.8);
        case "writer"->Map.of("draft","evidence-backed draft","citations",List.of("doi:1"));
        case "reviewer"->Map.of("approved",true,"issues",List.of(),"final","final report");
        default->throw new IllegalArgumentException();});}

    @Test void roleProtocolsAndPermissionsAreExplicit() {
        assertEquals(4,MultiAgent.ROLES.size());assertFalse(MultiAgent.ROLES.get("researcher").allowedTools().contains("spawn_subagent"));
        assertEquals(List.of("approved","issues","final"),MultiAgent.ROLES.get("reviewer").outputFields());
        assertThrows(IllegalArgumentException.class,()->MultiAgent.parseRoleResult("p","planner","{\"plan\":[]}"));
    }

    @Test void agentIdentityStatusAndResultPersist() throws Exception {
        ResearchRuntime runtime=new ResearchRuntime(root);MultiAgent.AgentTeam team=new MultiAgent.AgentTeam(runtime,(role,prompt,session,trace)->payload(role));runtime.bindTeam(team);
        assertTrue(team.spawnSubagent("reader","researcher","find",null).contains("spawned"));team.thread("reader").join(2000);
        assertEquals("completed",team.record("reader").status);assertTrue(team.collectResults().contains("finding"));
    }

    @Test void concurrencyLimitCancellationAndReassignmentWork() throws Exception {
        CountDownLatch release=new CountDownLatch(1);AtomicInteger calls=new AtomicInteger();ResearchRuntime runtime=new ResearchRuntime(root);
        MultiAgent.AgentTeam team=new MultiAgent.AgentTeam(runtime,(role,prompt,session,trace)->{calls.incrementAndGet();release.await(1,TimeUnit.SECONDS);return payload(role);},1,60);runtime.bindTeam(team);
        assertTrue(team.spawnSubagent("one","researcher","task",null).contains("spawned"));assertTrue(team.spawnSubagent("two","writer","task",null).contains("maximum 1"));
        assertTrue(team.cancelSubagent("one").contains("cancellation requested"));release.countDown();team.thread("one").join(2000);assertEquals("cancelled",team.record("one").status);

        AtomicInteger failures=new AtomicInteger();MultiAgent.AgentTeam retry=new MultiAgent.AgentTeam(runtime,(role,prompt,session,trace)->{if(failures.getAndIncrement()==0)throw new IllegalStateException("temporary");return payload(role);});
        retry.spawnSubagent("failed","researcher","task",null);retry.thread("failed").join(2000);assertTrue(retry.reassignFailed("failed","backup").contains("spawned"));retry.thread("backup").join(2000);assertEquals("completed",retry.record("backup").status);
    }

    @Test void endToEndWorkflowHasCheckpointsAndEvidence() {
        ResearchRuntime runtime=new ResearchRuntime(root);MultiAgent.AgentTeam team=new MultiAgent.AgentTeam(runtime,(role,prompt,session,trace)->payload(role));runtime.bindTeam(team);
        MultiAgent.WorkflowState state=team.runWorkflow("Agent memory 如何评测？");assertEquals("completed",state.status);
        assertEquals(List.of("planner","researcher","writer","reviewer"),state.tasks.stream().map(t->t.role).toList());assertTrue(state.tasks.stream().allMatch(t->t.status.equals("completed")));
        assertEquals(4,team.store.listSnapshots(state.id).size());assertEquals("doi:1",runtime.memory().loadWorking(state.id).evidence().get(0).get("source"));
    }

    @Test void oneRoleFailureRetriesAndWorkflowContinues() {
        Map<String,Integer> attempts=new java.util.HashMap<>();ResearchRuntime runtime=new ResearchRuntime(root);
        MultiAgent.AgentTeam team=new MultiAgent.AgentTeam(runtime,(role,prompt,session,trace)->{attempts.merge(role,1,Integer::sum);if(role.equals("researcher")&&attempts.get(role)==1)throw new IllegalStateException("temporary");return payload(role);});
        assertEquals("completed",team.runWorkflow("question").status);assertEquals(2,attempts.get("researcher"));
    }

    @Test void failedStepRollsBackAndCanResume() {
        ResearchRuntime runtime=new ResearchRuntime(root);MultiAgent.AgentTeam team=new MultiAgent.AgentTeam(runtime,(role,prompt,session,trace)->{if(role.equals("researcher"))throw new IllegalStateException("offline");return payload(role);});
        MultiAgent.WorkflowState paused=team.runWorkflow("question");assertEquals("paused",paused.status);assertEquals("completed",paused.tasks.get(0).status);assertEquals("pending",paused.tasks.get(1).status);
        team.setRunner((role,prompt,session,trace)->payload(role));assertEquals("completed",team.resumeWorkflow(paused.id).status);
    }

    @Test void conflictsNeedEvidenceAndApprovedConclusionCanBePromoted() {
        ResearchRuntime runtime=new ResearchRuntime(root);MultiAgent.AgentTeam team=new MultiAgent.AgentTeam(runtime,(a,b,c,d)->"");runtime.bindTeam(team);
        assertThrows(IllegalArgumentException.class,()->team.arbitrator.submit("Q","A","one",List.of(),0.9));
        var left=team.arbitrator.submit("Which model?","Use model A","one",List.of(new MultiAgent.Evidence("benchmark","doi:a",0.9)),0.9);
        var right=team.arbitrator.submit("Which model?","Use model B","two",List.of(new MultiAgent.Evidence("benchmark","doi:b",0.5)),0.5);
        assertEquals(2,team.arbitrator.conflicts("Which model?").size());var decision=team.arbitrator.arbitrate(List.of(left.id(),right.id()));assertEquals("approved",decision.status());
        String memory=team.arbitrator.promote(decision.id(),runtime.memory());assertEquals(memory,runtime.memory().recall("model A").get(0).id());
    }

    @Test void closeConflictRequiresHumanApproval() {
        ResearchRuntime runtime=new ResearchRuntime(root);MultiAgent.AgentTeam team=new MultiAgent.AgentTeam(runtime,(a,b,c,d)->"");runtime.bindTeam(team);
        var left=team.arbitrator.submit("Q","A","one",List.of(new MultiAgent.Evidence("x","s1",0.8)),0.8);var right=team.arbitrator.submit("Q","B","two",List.of(new MultiAgent.Evidence("x","s2",0.75)),0.75);
        var decision=team.arbitrator.arbitrate(List.of(left.id(),right.id()));assertEquals("human_required",decision.status());assertThrows(IllegalArgumentException.class,()->team.arbitrator.promote(decision.id(),runtime.memory()));
        assertTrue(runtime.resolveConflict(decision.id(),right.id()).contains("human approval"));runtime.approvals().reviewApproval(decision.approvalId(),true);assertTrue(runtime.resolveConflict(decision.id(),right.id()).contains("approved"));
    }
}
