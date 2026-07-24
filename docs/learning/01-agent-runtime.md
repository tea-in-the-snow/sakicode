# M1 学习讲义：Agent Runtime 状态机

## 1. 这次改造解决了什么

v1 的 `while True` 能运行，但“现在进行到了哪里”只隐含在 Python 调用栈和局部变量
里。遇到 API 失败、审批等待、用户中断或未来的进程恢复时，外部无法可靠判断任务状态。

显式状态机把这个问题拆成三个概念：

- **状态**：此刻处于哪个稳定阶段，例如正在请求模型；
- **事件/原因**：为什么发生变化，例如模型返回了工具调用；
- **转换约束**：哪些变化合法，例如 `idle → completed` 不合法。

它不是为了替代 `while` 循环，而是为循环增加一个可检查的控制平面。

## 2. 本项目的核心状态轨迹

一次无需工具的请求：

```text
idle → requesting_model → completed
```

一次读取文件后再回答的请求：

```text
idle → requesting_model → executing_tools
     → requesting_model → completed
```

一次需要审批的写文件请求：

```text
idle → requesting_model → executing_tools
     → waiting_approval → executing_tools
     → requesting_model → completed
```

注意：审批被拒绝并不等于整个 Agent 失败。拒绝结果会作为 tool message 返回给模型，
模型可以改用只读方案或向用户解释，因此状态会回到 `executing_tools`，随后继续请求模型。

## 3. 必须维护的不变量

### Tool Calling 消息必须闭合

带 `tool_calls` 的 assistant message 后，每个 call id 都必须有对应的 tool message，
之后才能再次请求模型。否则许多兼容 OpenAI 的服务会拒绝请求。即使达到工具预算，
代码也应先为未执行的调用补一个错误 tool result，再进入 `limit_reached`。

### 终态与错误结果不是一回事

- 工具返回“文件不存在”是模型可处理的数据，不一定让 runtime 进入 `failed`；
- 模型 API 无法完成本轮请求，才进入 `failed`；
- 用户按 Ctrl-C 进入 `interrupted`；
- 预算耗尽进入 `limit_reached`。

区分这些终态后，未来恢复策略才能不同：API 失败可以重试，中断可以恢复，预算耗尽
可能需要用户提高上限。

### 转换必须集中校验

如果各模块直接赋值 `runtime.state = ...`，状态机很快会退化成一个普通字段。
所有变化都经过 `transition()`，才能统一拒绝非法边、记录原因，并在未来挂接 trace
或 checkpoint。

## 4. 安装并运行 SakiCode

以下命令都在项目根目录执行：

```bash
cd /home/shaoran/workspace/sakicode
uv sync --extra dev
```

默认配置使用 DeepSeek：

```bash
export DEEPSEEK_API_KEY="你的 API Key"
uv run sakicode
```

也可以把密钥写入项目根目录的 `.env`，避免每次打开终端都重新设置：

```dotenv
DEEPSEEK_API_KEY=你的 API Key
```

`.env` 已被 Git 忽略，但仍不要在终端截图、测试输出或提交记录中暴露真实密钥。

如果使用其他 OpenAI-compatible 服务，需要同时指定 endpoint 和模型，例如：

```bash
export OPENAI_API_KEY="你的 API Key"
export OPENAI_BASE_URL="服务商提供的 API Base URL"
uv run sakicode --model "服务商提供的模型名"
```

启动成功后会看到 `saki> `。输入 `exit` 或 `quit` 退出；一次请求执行过程中按
Ctrl-C 会中断当前 turn，但不会直接退出 REPL。

## 5. 在运行中体验状态机

REPL 内置 `/runtime` 命令。它不调用模型，而是打印当前状态和从启动以来的转换历史。

### 实验一：观察初始状态

刚启动时输入：

```text
saki> /runtime
Runtime state: idle
(no transitions yet)
```

这说明创建 Agent 并不等于开始执行任务，只有收到用户任务后才会离开 `idle`。

### 实验二：不调用工具的最短路径

输入：

```text
saki> 请不要调用任何工具，只回复“状态机实验完成”
saki> /runtime
```

历史末尾应类似：

```text
idle -> requesting_model: user turn started
requesting_model -> completed: model returned final response
```

这对应最短路径 `idle → requesting_model → completed`。

### 实验三：观察工具调用闭环

输入：

```text
saki> 请读取 README.md，并告诉我项目的运行命令
saki> /runtime
```

如果模型正确选择 `read_file`，历史中应出现：

```text
completed -> requesting_model: user turn started
requesting_model -> executing_tools: model requested tools
executing_tools -> requesting_model: tool results ready
requesting_model -> completed: model returned final response
```

第一条从 `completed` 开始，是因为同一个 Agent 正在开始第二个用户 turn。工具输出作为
tool message 回传后，Runtime 才能从 `executing_tools` 回到
`requesting_model`。

### 实验四：观察人工审批

输入一个只用于练习的写文件任务：

```text
saki> 请创建 runtime-demo.txt，内容为 hello runtime
```

出现 `Allow? [y/N]` 时先输入 `n`，然后执行 `/runtime`。历史中应出现：

```text
executing_tools -> waiting_approval: approval required for write_file
waiting_approval -> executing_tools: approval denied for write_file
```

文件不会被创建，但拒绝结果仍会回传给模型。你也可以再次执行任务并输入 `y`，对比
`approval granted` 路径；实验结束后自行删除 `runtime-demo.txt`。

模型行为具有随机性。如果它没有按提示调用预期工具，换用更明确的措辞重试即可。
状态机测试不依赖这种随机性。

### 实验五：离线验证，不消耗 API

运行：

```bash
uv run pytest tests/test_runtime.py tests/test_repl.py -vv
```

这些测试通过脚本化模型响应覆盖合法转换、非法转换、模型—工具闭环、审批拒绝和
`/runtime` 格式化逻辑，不访问网络，也不会消耗 token。

运行全部回归测试：

```bash
uv run pytest
```

## 6. 如何阅读本次代码

建议按这个顺序：

1. `src/sakicode/runtime.py`：看状态枚举、允许转换表、事件记录；
2. `tests/test_runtime.py`：先理解希望保证的行为；
3. `src/sakicode/agent.py::run_turn`：把循环分支映射回状态图；
4. `src/sakicode/agent.py::_execute_tool`：观察审批前后的状态变化；
5. `src/sakicode/repl.py::format_runtime`：观察状态如何暴露给用户。

测试使用 `ScriptedAgent` 替代真实模型响应，这属于 test double。它让测试只验证
控制逻辑，不受网络、模型随机性和 API 费用影响。

## 7. 你应该能回答的面试问题

1. 原来的循环已经能工作，为什么还需要状态机？
2. 为什么工具执行错误通常不进入 `failed`？
3. 为什么审批拒绝后还要继续把结果发给模型？
4. 如何保证 tool call 与 tool result 一一配对？
5. checkpoint 应保存当前瞬时状态，还是只保存安全恢复点？为什么？

第 5 题的建议答案是：检查点应标记安全恢复点，并避免直接从
`executing_tools` 重放非幂等操作；否则一次中断可能导致写文件或外部调用执行两遍。
这会在 M5 中通过 operation id、trace 与恢复策略继续实现。

## 8. 动手练习

先不要看实现，手画下面场景的轨迹：

> 模型请求写文件，用户拒绝；模型改为读取文件；读取成功后模型给出说明。

然后增加一个测试，断言轨迹中依次出现 `waiting_approval` 和第二次
`executing_tools`。再思考：如果用户在审批提示时按 Ctrl-C，应该从哪个状态进入
`interrupted`？

## 附录：面试问题参考答案

### 1. 原来的循环已经能工作，为什么还需要状态机？

`while` 循环只描述程序如何继续执行，当前阶段隐含在调用栈、局部变量和分支位置中。
这种实现可以完成短任务，但外部模块很难可靠回答以下问题：

- Agent 正在等待模型、执行工具，还是等待用户审批？
- 本轮为何结束，是正常完成、API 失败、用户中断，还是预算耗尽？
- 进程重启后应该从哪里恢复？
- 某个状态变化是否合法？

显式状态机把运行阶段建模为有限状态，把允许的转换集中定义，并记录每次转换的原因。
它提供了一个可观察、可验证、可持久化的控制平面。日志、权限审批、超时处理和
checkpoint 都可以建立在这个稳定边界上。

需要注意，状态机不会替代 Agent Loop。循环仍负责实际调度，状态机负责表达和约束
调度过程。

### 2. 为什么工具执行错误通常不进入 `failed`？

工具错误通常属于 Agent 可以继续处理的业务结果，例如：

- 文件不存在；
- 搜索没有匹配项；
- 测试命令返回非零退出码；
- 用户拒绝写文件；
- 工具参数格式不正确。

这些结果应该以 tool message 返回给模型，让模型修正路径、修改参数、选择替代方案，
或向用户解释失败原因。如果遇到任何工具错误都立即终止 Runtime，Agent 就无法根据
测试和编译反馈迭代修复，这与 Coding Agent 的目标相冲突。

`failed` 应表示本轮控制流程无法继续，例如模型 API 持续不可用、内部状态不一致，
或 Runtime 自身发生不可恢复异常。

因此需要区分两层错误：

- **Tool-level error**：一次操作失败，Agent 仍可推理和恢复；
- **Runtime-level failure**：调度循环无法继续，本轮进入终态。

### 3. 为什么审批拒绝后还要继续把结果发给模型？

模型发出工具调用后，对话协议要求对应的 tool call 获得一个 tool result。用户拒绝
审批也是这次调用的结果，应返回类似 `permission denied by user` 的结构化信息。

这样做有两个原因：

1. 保持 assistant tool call 与 tool result 一一配对，避免下一次模型请求因消息序列
   不合法而失败；
2. 让模型知道限制来自权限策略，从而选择只读工具、缩小操作范围，或者请求用户提供
   其他方案。

审批拒绝表示“这次操作不允许”，不一定表示“整个任务无法完成”。因此 Runtime 会从
`waiting_approval` 回到 `executing_tools`，记录拒绝结果，再进入
`requesting_model` 让模型继续决策。

### 4. 如何保证 tool call 与 tool result 一一配对？

模型返回带 `tool_calls` 的 assistant message 后，Runtime 应保存其中每个稳定的
call id。随后，无论工具成功、执行报错、审批被拒绝还是预算耗尽，都必须为每个 call
id 追加且只追加一个对应的 tool message，最后才能再次请求模型。

核心不变量可以写成：

```text
对于每个已提交到消息历史的 tool_call_id：
恰好存在一个相同 tool_call_id 的 tool result，
并且 result 位于下一条模型请求之前。
```

本项目在工具预算耗尽时，也会为跳过的调用生成错误结果，而不是直接丢弃调用。测试时
应覆盖一次返回多个 tool call、部分失败、审批拒绝和达到预算上限等场景。

更完整的实现还应在请求模型前增加消息历史校验器，检查：

- tool call id 非空且在当前 assistant message 中唯一；
- result 引用的 id 确实存在；
- 每个调用只有一个结果；
- 不存在尚未闭合的调用。

### 5. Checkpoint 应保存当前瞬时状态，还是只保存安全恢复点？为什么？

应该保存足够完整的瞬时信息用于诊断，但自动恢复只能从明确的安全恢复点继续，不能
不加判断地重放任意瞬时状态。

例如，进程在 `executing_tools` 时崩溃，外部命令可能已经执行成功，只是结果还没写入
checkpoint。如果恢复后直接重新调用工具，写文件、发送请求或创建资源等非幂等操作
可能执行两次。

比较安全的设计是：

- 在请求模型前、完整接收模型响应后、所有工具结果闭合后建立恢复点；
- 为工具调用分配 operation id，并记录 `planned / started / completed` 状态；
- 对天然幂等的只读操作允许安全重试；
- 对非幂等操作先核查外部效果，无法确认时请求人工决策；
- 使用临时文件加原子替换写入 checkpoint，避免读取半写数据；
- 保存 schema 版本和工作区身份，但不保存 API Key 等秘密。

因此 checkpoint 不是简单地序列化 `runtime.state`，而是持久化恢复所需的消息、
任务摘要、操作日志、权限决定和安全点信息。恢复逻辑还必须根据操作是否幂等以及工具
是否已经产生外部效果来决定重试、跳过或请求审批。
