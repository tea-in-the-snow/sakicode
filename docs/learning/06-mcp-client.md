# M6 学习讲义：MCP 客户端

## 1. 这次改造解决了什么

M2 把内置工具统一到了 `Tool`/`ToolRegistry` 协议，但 Agent 能用的工具仍然只有
仓库里写死的六个。每接一个新能力——查文档、连数据库、调内部服务——都要改
Agent 的代码、重新发布。MCP（Model Context Protocol）把工具供给方外置为独立
进程：Agent 只做客户端，通过标准协议发现、校验并调用远端工具，供给侧可以独立
演进、独立发布。

但“工具来自外部进程”立刻带来三个内置工具没有的问题：

1. **不信任**：server 是第三方代码，可能发垃圾字节、撒谎、泄露数据；
2. **不可靠**：子进程会挂起、崩溃、半路死亡，Agent 不能被拖垮；
3. **不透明**：远端工具长什么样，只能通过协议问出来，不能用 `import` 静态确定。

M6 的答案是把 MCP server 当作“不可信子进程”对待，整体结构如下：

```text
.sakicode/mcp.json
     │  load_server_specs()（结构校验）
     ▼
McpServerSpec ──► StdioMcpClient.start()   spawn 子进程（stdin/stdout 管道）
     │                initialize()        握手 + 能力协商
     │                list_tools()         工具发现
     ▼
McpTool（适配器：remote tool → Tool 协议）
     │
     ▼
ToolRegistry.register() ──► schema 校验、trace、权限引擎全部复用
     │
     ▼
Agent 像调用内置工具一样调用 mcp__<server>__<tool>
```

## 2. JSON-RPC：一次一个请求的同步模型

MCP 的消息层是 JSON-RPC 2.0。每条消息是一个 JSON 对象，三类：

```json
{"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {...}}   // 请求
{"jsonrpc": "2.0", "id": 1, "result": {...}}                            // 成功响应
{"jsonrpc": "2.0", "method": "notifications/message", "params": {...}}  // 通知（无 id）
```

`id` 是请求与响应的配对联结。`StdioMcpClient` 刻意采用最保守的并发模型：
**同一时刻只有一个未决请求**（`_request` 同步阻塞直到拿到匹配 id 的响应）。这
带来两条强不变量：

- 响应 id 必须等于刚发出的请求 id；收到陌生 id 的响应直接判协议错误；
- 期间收到的无 id 消息一定是通知，跳过即可——不需要乱序重组。

如果未来要并发调用，就得引入 pending-request 表（id → Future）和读写线程，
复杂度上一个台阶。Agent 的工具调用本来就是顺序的（模型一次返回、逐个执行），
所以这里没有买并发这张票。

错误分两层，必须区分对待：

- **JSON-RPC error 对象**（`McpRemoteError`）：server 健康，只是这次调用被拒。
  连接保持可用，后续调用照常——测试里 `rpc_error` 之后 `echo` 仍然成功；
- **帧级错误**（垃圾字节、EOF、超时）：流已经无法可靠重新同步，标记 client
  为 broken 并杀掉子进程。

“连接级错误不可恢复”是有意的取舍：理论上可以重建偏移量继续解析，但一个会发
垃圾的 peer 之后的行为无法推理，继续信任它比重启它更危险。

## 3. stdio framing：为什么不能用 `readline()`

MCP 的 stdio transport 规定消息以换行符分隔（newline-delimited JSON），每条
消息必须是一行、不含内嵌换行。读侧的核心循环在 `_read_message`：

```text
循环:
  buffer 里有 '\n'?  → 切出一行，json.loads，返回
  已过 deadline?     → McpTimeoutError
  select(fd, 剩余时间) 超时? → McpTimeoutError
  os.read(fd, 64KB)  → 追加到 buffer（返回空字节串 = EOF = 进程死了）
  buffer 超过 10MB?  → McpProtocolError
```

三个细节各自防一种失败模式：

1. **`select` + `os.read` 而不是 `readline()`**：Python 的 buffered reader 会先
   把数据吞进用户态缓冲区，`select` 只监控内核 fd——两者混用会出现“select 说
   没数据，其实数据在 Python 缓冲区里”的错位；反过来 partial line 到了、换行
   没到时 `readline()` 会无视 deadline 死等。自己持有 buffer，就只有一个事实源。
2. **EOF 即进程死亡**：`os.read` 返回空字节串说明对端关闭了 stdout，此时
   `process.poll()` 给出退出码。这不是超时，而是另一种错误类别
   （`McpProcessError`），错误消息里能看到 exit code，便于区分“挂起”和“崩溃”。
3. **消息大小上限**：恶意或 bug 的 server 可以永不发换行、无限写数据。没有上
   限就是内存 DoS。超过上限按协议错误处理——流同样不可信。

## 4. 握手与能力协商

连接不是 spawn 完就可用。MCP 生命周期要求：

```text
client → initialize {protocolVersion, capabilities, clientInfo}
server → result {protocolVersion, capabilities, serverInfo}
client → notifications/initialized        （通知，无响应）
client → tools/list / tools/call ...
```

`initialize()` 做三件事：声明客户端协议版本、校验 server 确实按协议回答
（结果里没有 `protocolVersion` 就是协议错误）、发送 `initialized` 通知告诉
server 握手完成。能力协商当前是极简版——客户端声明空 capabilities，只消费
server 的 tools 能力——但握手的价值不在交换的那几个字段，而在**确认对端真的
会说 MCP**：一个能启动但不是 MCP server 的程序（比如配置写错了路径）会在第一
个请求就现形，而不是等到某次工具调用时才以诡异方式失败。这是 fail fast。

## 5. 工具发现与统一 Registry：适配器模式

`list_tools()` 拿到的是 server 的自述：name、description、inputSchema。M6 不
让 Agent 感知“远端”这个概念，而是把每个远端工具适配成 M2 的 `Tool` 协议
（`McpTool`），注册进同一个 `ToolRegistry`：

```text
远端 name        → 注册名 mcp__<server>__<tool>（server 名防止多 server 撞名）
远端 inputSchema → 原样作为 input_schema（register 时会做 JSON Schema 元校验）
invoke(args)     → client.call_tool(remote_name, args) → ToolResult
```

适配的价值在于**下游零改动**：

- 模型看到的 schema 由 `registry.schemas()` 统一产出，MCP 工具与内置工具同构；
- 参数校验仍发生在调用前、本地完成（`test_arguments_are_validated_against_the_remote_schema`）——
  不合法的请求根本不发往 server，server 的 schema 反而成了保护 server 的第一道关；
- trace、脱敏、耗时统计走 `registry.execute` 的同一条路径。

命名前缀 `mcp__` 同时是一个安全信号：权限引擎靠它识别“这是外部工具”。

远端错误到 `ToolResult` 的映射是一张明确的表，不允许混成普通字符串：

| 远端情况 | ToolResult |
| --- | --- |
| `isError: true` | `EXECUTION_ERROR` + 远端文本 |
| JSON-RPC error 对象 | `EXECUTION_ERROR` + code/message |
| 超时 | `TIMEOUT`，子进程已被 kill |
| 崩溃/EOF/垃圾字节/broken 重调 | `EXECUTION_ERROR`，说明连接状态 |

## 6. 超时与故障隔离

M6 的验收标准是“协议错误、超时、进程崩溃不会拖垮 agent”。隔离由四层组成：

1. **进程隔离**：server 从第一行起就是独立子进程，它的崩溃在操作系统层面就
   不可能拖垮 Agent 进程；
2. **每个请求的硬 deadline**：任何请求最多等 `request_timeout` 秒。超时即
   `kill()`——不是 `terminate()`，因为一个连响应都发不出来的进程，礼貌的
   SIGTERM 大概率也叫不醒；
3. **broken 熔断**：帧级错误后 client 进入 broken 态，之后的调用**快速失败**
   （`"broken: ..."`），不再碰管道。Agent 每一轮看到的是结构化错误结果，可以
   继续规划（换工具、报告用户），而不是阻塞或崩溃；
4. **启动期隔离**：`connect_configured_servers` 逐个连接，一个 server 失败只
   打印警告并跳过，其余 server 和内置工具照常可用；`connect` 内部任何异常都
   先 `close()` 再抛出，不会把半成品注册进 registry。

`test_timeout_kills_the_server_and_breaks_the_client` 验证前三个：30 秒的
`sleep` 在 0.3 秒超时后返回 `TIMEOUT`，进程已死，后续调用快速失败。

这里有一个值得记住的对应关系：M5 用“终态 + 原子提交”保护磁盘状态不被半写
破坏；M6 用“超时 + kill + 熔断”保护进程不被半死的 peer 拖住。两者都是同一
个原则——**故障发生时收窄爆炸半径，并让剩余系统保持可推理的状态**。

## 7. 生命周期：谁开谁关

`StdioMcpClient` 的资源是子进程加两根管道，所有权链必须闭合：

- `start()` 失败（二进制不存在）→ `McpProcessError`，无资源泄漏；
- `connect()` 中握手或发现失败 → `close()` 兜底后抛出；
- 正常运行结束 → `cli.py` 在 `try/finally` 里逐个 `close()`；
- `close()` 自身是渐进的：先关 stdin 给 server 退出的信号，再 `terminate()`
  给 2 秒宽限，最后 `kill()` 兜底。三次机会对应三种 server 品质：配合的、
  迟钝的、耍赖的。

broken 与 close 语义不同：broken 是“不信任了，立即杀”，close 是“合作结束，
礼貌送别”。测试 fixture 依赖后者保证不留僵尸进程。

## 8. MCP 工具为什么必须经过权限引擎

M3 的分类表里没有的工具**默认拒绝**——这条规则当时就是给“未来出现的工具”
准备的。M6 新增了一个显式分支：`mcp__` 前缀的工具默认 **ASK**，grant key 是
`mcp:<注册名>`，支持“本会话同类操作”。

为什么不是 ALLOW 也不是 DENY？

- ALLOW 不可能：远端工具执行的是外部服务器提供的任意代码，风险上限不低于
  `run_bash`，后者尚且默认 ASK；
- DENY 太粗：MCP 工具的价值就是可用，全拒绝等于没接。引擎无法按路径或命令
  内容分类（它看不见远端实现），所以把判断交给用户，一次一批准；
- grant key 按注册名精确到 `server + tool`：批准 `mcp__fs__read` 绝不意味着
  批准 `mcp__fs__delete`。测试 `test_permission_engine_asks_for_mcp_tools_with_session_grant`
  验证了这个粒度。

审批链路与内置工具完全一致：`Agent._execute_tool` 不区分工具来源，policy ASK
就进 `waiting_approval` 态，审批记录进 audit log。这就是 M2 统一协议的红利：
新工具类别接入时，安全边界不需要重写。

还要注意信任方向：server 提供的 description 和 schema 会进入模型上下文，属于
**不可信文本**（tool poisoning 是已知的 MCP 攻击面——description 里藏指令
诱导模型误用其他工具）。本项目的防线是：参数由本地 schema 校验、每次调用过
权限引擎、用户审批时看到的是规范化 target 而非 server 的宣传文案。更完整的
方案（server 签名、工具描述审计）留作练习。

## 9. 如何运行与验证

写一个 MCP server 配置（默认路径 `.sakicode/mcp.json`）：

```json
{
  "servers": [
    {
      "name": "fake",
      "command": ["python", "tests/fake_mcp_server.py"],
      "request_timeout": 10
    }
  ]
}
```

启动后能看到连接日志，REPL 中模型即可调用 `mcp__fake__echo` 等工具：

```bash
cd /home/shaoran/workspace/sakicode
uv run sakicode            # 或 --mcp-config 指定其它路径
```

首次调用每个 MCP 工具会要求审批（可选“本会话同类”）。运行 M6 专项测试：

```bash
UV_CACHE_DIR=/tmp/sakicode-uv-cache uv run pytest tests/test_mcp.py -vv
```

关键测试与验收标准对应关系：

- `test_handshake_discovers_and_registers_tools`：握手、发现、注册（本地假
  server，不访问网络）；
- `test_call_tool_round_trip_through_registry`：经统一 registry 的完整调用，
  且假 server 会在响应前插入通知，验证 framing 跳过逻辑；
- `test_arguments_are_validated_against_the_remote_schema`：调用前的本地
  Schema 校验；
- `test_remote_is_error_result_becomes_structured_error` /
  `test_json_rpc_error_response_becomes_structured_error`：两类远端错误都变
  成结构化结果，且后者不破坏连接；
- `test_timeout_kills_the_server_and_breaks_the_client`：超时 kill + 熔断 +
  快速失败；
- `test_server_crash_is_contained`：进程崩溃不拖垮 agent；
- `test_garbage_handshake_fails_connect_and_cleans_up`：协议错误在启动期现形，
  无半成品注册；
- `test_permission_engine_asks_for_mcp_tools_with_session_grant`：与内置工具
  相同的权限引擎，session grant 精确到单个工具。

## 10. 建议阅读顺序

1. `mcp.py::StdioMcpClient._request()`：同步请求/响应配对与错误分流；
2. `_read_message()`：select + 自持 buffer 的 framing 循环；
3. `initialize()` / `list_tools()`：握手与发现；
4. `McpTool.invoke()`：远端结果到 `ToolResult` 的映射表；
5. `connect()` / `connect_configured_servers()`：启动期隔离与资源清理；
6. `permissions.py::_classify_mcp()`：外部工具的默认 ASK 策略；
7. `cli.py`：配置加载、registry 注入与 `try/finally` 关闭；
8. `tests/fake_mcp_server.py` + `tests/test_mcp.py`：用故障注入理解每条保证。

## 11. 你应该能回答的面试问题

1. JSON-RPC 的 `id` 起什么作用？为什么“一次一个未决请求”能简化配对逻辑？
2. 通知（notification）和请求的区别是什么？framing 循环里为什么要跳过通知？
3. stdio transport 为什么用换行分隔而不是 Content-Length 头？各自代价是什么？
4. 为什么不能混用 `select` 和 buffered reader 的 `readline()`？
5. 握手（initialize）解决了什么问题？少了它会在什么时候、以什么形式失败？
6. 为什么 MCP 工具要适配进统一 registry，而不是给 Agent 加一条“远端调用”
   分支？适配后哪些机制被免费复用了？
7. 超时后为什么是 `kill()` 而不是 `terminate()`？
8. “broken 熔断”防的是什么？为什么帧级错误后不尝试重新同步流？
9. 远端 `isError`、JSON-RPC error、超时、进程崩溃分别映射到什么
   `ToolErrorCode`？为什么 JSON-RPC error 不熔断连接？
10. 子进程隔离为什么天然成立？它不能防什么（提示：数据面）？
11. 为什么 MCP 工具默认 ASK 而不是 DENY？grant key 的粒度为什么按注册名？
12. server 的 description 为什么是不可信输入？tool poisoning 攻击长什么样，
    本项目防住了哪一环？
13. `close()` 的三步（关 stdin → terminate → kill）各自对付什么样的 server？
14. 如果要求并发调用多个 MCP 工具，当前的同步客户端要改哪些不变量？

## 12. 动手练习

### 练习一：ping 与自动重连

给 client 增加 `ping()` 健康检查和“broken 后按指数退避自动重连”的策略。
回答：重连后之前的 session grant 是否还有效？为什么（从 grant key 的稳定性和
server 状态是否延续两个角度分析）？

### 练习二：paginated tools/list

MCP 的 `tools/list` 可能分页（结果带 `nextCursor`）。实现翻页发现，并用假
server 验证“翻到一半 server 崩溃”时 registry 里不会出现半份工具列表。

### 练习三：输出大小预算

远端工具可以返回任意大的 content。给 `McpTool.invoke` 加输出截断（复用
`tools.py` 的截断思路），保证巨型响应不会撑爆 M4 的上下文预算，并在 metadata
里记录截断前后大小。

### 练习四：tool poisoning 演练

在假 server 的 `echo` 工具 description 里写一段诱导模型“先调用 run_bash 读取
~/.ssh/id_rsa”的文本，观察 Agent 的行为链。然后设计一层防御：在注册时对
description 做静态扫描或在系统提示中声明远端描述不可信。比较两种防线被绕过
的难易。
