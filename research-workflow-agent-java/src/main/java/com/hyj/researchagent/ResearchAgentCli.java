package com.hyj.researchagent;

import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.Scanner;
import java.util.Set;

/** Research Workflow Agent 的多轮命令行入口。 */
public final class ResearchAgentCli {
    private ResearchAgentCli(){}
    public static void main(String[] args)throws Exception{Map<String,String> options=arguments(args);Map<String,String> env=new HashMap<>(System.getenv());loadEnv(Path.of(".env"),env);Path workspace=Path.of(options.getOrDefault("workspace",".")).toAbsolutePath().normalize();if(!Files.isDirectory(workspace))throw new IllegalArgumentException("Workspace does not exist");
        String model=options.getOrDefault("model",env.get("MODEL_ID"));if(model==null)throw new IllegalArgumentException("MODEL_ID is required");ResearchRuntime runtime=new ResearchRuntime(workspace);Resilience.ModelClient client=new AnthropicResearchClient(env.get("ANTHROPIC_API_KEY"),env.get("ANTHROPIC_BASE_URL"));
        MultiAgent.AgentRunner runner=(role,prompt,session,trace)->ResearchAgent.agentLoop(client,new ArrayList<>(List.of(Map.of("role","user","content",prompt))),runtime,model,session,role,trace,"multi","subtask",MultiAgent.ROLES.get(role).allowedTools(),12);
        MultiAgent.AgentTeam team=new MultiAgent.AgentTeam(runtime,runner);runtime.bindTeam(team);List<Map<String,Object>> history=new ArrayList<>();Scanner scanner=new Scanner(System.in);System.out.println("Research Agent workspace: "+workspace);
        while(true){System.out.print("research >> ");if(!scanner.hasNextLine())return;String query=scanner.nextLine().trim();if(query.isBlank()||Set.of("q","quit","exit").contains(query.toLowerCase()))return;history.add(Map.of("role","user","content",query));String answer=ResearchAgent.agentLoop(client,history,runtime,model);System.out.println(answer);}}
    private static Map<String,String> arguments(String[] args){Map<String,String> result=new HashMap<>();for(int i=0;i<args.length-1;i++)if(args[i].startsWith("--"))result.put(args[i].substring(2),args[++i]);return result;}
    static void loadEnv(Path path,Map<String,String> env)throws Exception{if(!Files.exists(path))return;for(String line:Files.readAllLines(path)){String text=line.trim();if(text.isBlank()||text.startsWith("#")||!text.contains("="))continue;int at=text.indexOf('=');env.put(text.substring(0,at).trim(),text.substring(at+1).trim().replaceAll("^[\"']|[\"']$",""));}}
}
