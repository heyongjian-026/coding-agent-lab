package com.hyj.researchagent;

import com.fasterxml.jackson.core.type.TypeReference;

import java.io.BufferedReader;
import java.io.BufferedWriter;
import java.io.InputStreamReader;
import java.io.OutputStreamWriter;
import java.nio.charset.StandardCharsets;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;

/** MCP 传输抽象、工具发现、名称规范化和调用结果提取。 */
public final class McpSupport {
    private McpSupport() {}
    public interface Transport {
        Map<String,Object> request(String method,Map<String,Object> params) throws Exception;
        default void notify(String method,Map<String,Object> params) throws Exception {}
        default void close() throws Exception {}
    }

    /**
     * 持久化换行分隔 JSON-RPC stdio 传输。命令采用 argv，不经过 shell；请求串行化以保持响应 ID 配对。
     */
    public static final class StdioTransport implements Transport {
        private final Process process;private final BufferedWriter writer;private final BufferedReader reader,error;private int requestId;
        public StdioTransport(List<String> command,Path cwd,Map<String,String> environment)throws Exception{
            if(command==null||command.isEmpty()||command.stream().anyMatch(v->v==null||v.isBlank()))throw new IllegalArgumentException("MCP command must be a non-empty argv list");
            ProcessBuilder builder=new ProcessBuilder(command).directory(cwd.toAbsolutePath().normalize().toFile());if(environment!=null)builder.environment().putAll(environment);process=builder.start();
            writer=new BufferedWriter(new OutputStreamWriter(process.getOutputStream(),StandardCharsets.UTF_8));reader=new BufferedReader(new InputStreamReader(process.getInputStream(),StandardCharsets.UTF_8));error=new BufferedReader(new InputStreamReader(process.getErrorStream(),StandardCharsets.UTF_8));
        }
        public synchronized Map<String,Object> request(String method,Map<String,Object> params)throws Exception{int id=++requestId;send(Map.of("jsonrpc","2.0","id",id,"method",method,"params",params));String line;
            while((line=reader.readLine())!=null){Map<String,Object> message=JsonFiles.JSON.readValue(line,new TypeReference<>(){});if(!String.valueOf(id).equals(String.valueOf(message.get("id"))))continue;if(message.containsKey("error"))throw new IllegalStateException(String.valueOf(message.get("error")));return message.get("result") instanceof Map<?,?>?JsonFiles.map(message.get("result")):Map.of();}
            StringBuilder details=new StringBuilder();while(error.ready()){String value=error.readLine();if(value!=null)details.append(value).append('\n');}throw new IllegalStateException("MCP server stopped: "+details);}
        public synchronized void notify(String method,Map<String,Object> params)throws Exception{send(Map.of("jsonrpc","2.0","method",method,"params",params));}
        private void send(Object value)throws Exception{writer.write(JsonFiles.JSON.writer().without(com.fasterxml.jackson.databind.SerializationFeature.INDENT_OUTPUT).writeValueAsString(value));writer.newLine();writer.flush();}
        public void close(){if(process.isAlive())process.destroy();}
    }
    public record Tool(String exposedName,String server,String originalName,String description,
                       Map<String,Object> inputSchema,boolean readOnly) {}

    public static final class McpClient {
        private final String name;private final Transport transport;private final Map<String,Tool> tools=new LinkedHashMap<>();
        public McpClient(String name,Transport transport){this.name=normalize(name);this.transport=transport;}
        /** 初始化 MCP 会话并发现工具，未声明 readOnlyHint 的工具按有副作用处理。 */
        public List<Tool> connect() throws Exception {
            transport.request("initialize",Map.of("protocolVersion","2025-03-26","capabilities",Map.of(),"clientInfo",Map.of("name","research-agent-java","version","1.0")));
            transport.notify("notifications/initialized",Map.of());Map<String,Object> response=transport.request("tools/list",Map.of());
            List<Tool> result=new ArrayList<>();Object raw=response.get("tools");if(raw instanceof List<?> list)for(Object value:list){Map<String,Object> item=JsonFiles.map(value);String original=String.valueOf(item.get("name"));
                Map<String,Object> annotations=item.get("annotations") instanceof Map<?,?>?JsonFiles.map(item.get("annotations")):Map.of();boolean readOnly=Boolean.TRUE.equals(annotations.get("readOnlyHint"));
                Tool tool=new Tool("mcp__"+name+"__"+normalize(original),name,original,String.valueOf(item.getOrDefault("description","")),item.get("inputSchema") instanceof Map<?,?>?JsonFiles.map(item.get("inputSchema")):Map.of("type","object"),readOnly);tools.put(tool.exposedName(),tool);result.add(tool);}
            return result;
        }
        public String call(String exposedName,Map<String,Object> args)throws Exception{Tool tool=tools.get(exposedName);if(tool==null)throw new IllegalArgumentException("Unknown MCP tool: "+exposedName);
            Map<String,Object> response=transport.request("tools/call",Map.of("name",tool.originalName(),"arguments",args));Object content=response.get("content");if(content instanceof List<?> list){List<String> text=new ArrayList<>();for(Object value:list){Map<String,Object> block=JsonFiles.map(value);if("text".equals(block.get("type")))text.add(String.valueOf(block.get("text")));}if(!text.isEmpty())return String.join("\n",text);}return JsonFiles.toJson(response);}
        public Tool tool(String exposed){return tools.get(exposed);} public List<Tool> tools(){return new ArrayList<>(tools.values());} public String name(){return name;}
    }
    public static String normalize(String value){return value.toLowerCase(Locale.ROOT).replaceAll("[^a-z0-9_-]+","_").replaceAll("^_+|_+$","");}
}
