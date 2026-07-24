# M2 学习讲义：工具协议与可观测性

## 1. 为什么不能继续使用全局函数表

M1 中的 Agent Loop 通过工具名查找 Python 函数，所有结果都是字符串。这对六个内置
工具足够，但会逐渐产生四类问题：

- Agent、权限系统和未来的 MCP 客户端分别维护一份工具信息；
- 模型生成的参数没有在执行前按 Schema 校验；
- 超时、非零退出码和文件错误都只能从字符串中猜测；
- 无法统一记录耗时、截断情况、调用参数和错误类别。

M2 把工具调用收敛到 `ToolRegistry`。内置工具、未来的 MCP 工具和测试工具只要实现
相同协议，就能复用发现、校验、计时、错误隔离和 trace。

## 2. 当前调用链

```text
模型返回 tool call
        │
        ▼
Agent 解析 JSON ──失败──> ToolResult(invalid_arguments)
        │
        ▼
ToolRegistry JSON Schema 校验
        │
        ▼
权限确认（M3 会进一步增强）
        │
        ▼
ToolRegistry 查找工具 → 再次校验 → Tool.invoke()
        │                   │           │
        │                   └─拒绝参数   └─捕获异常
        ▼
ToolResult(content, error_code, duration_ms, metadata)
        │
        ├─> ToolTrace（参数脱敏，不保存工具输出）
        └─> JSON tool message 返回给模型
```

这里最重要的边界是：工具内部负责业务语义，Registry 负责所有工具共有的运行时语义。

## 3. Tool、FunctionTool 与依赖倒置

`Tool` 是 Python `Protocol`，要求实现：

- `name`：稳定、唯一的工具名；
- `description`：提供给模型的用途描述；
- `input_schema`：参数的 JSON Schema；
- `requires_confirmation`：当前的基础审批标记；
- `invoke(arguments)`：执行并返回 `ToolResult`。

`Protocol` 使用结构化子类型：一个对象只要具有这些成员，就能被 Registry 使用，
不必继承某个共同基类。它适合 MCP adapter，因为第三方工具可以在边界处被包装，
而不需要修改其原始实现。

`FunctionTool` 则是适配器，把现有的 Python 函数包装成 `Tool`。因此文件读写函数仍然
只关心文件操作，不需要知道 OpenAI tool schema、trace 或 Agent 消息格式。

这体现了依赖倒置：

```text
Agent ──依赖──> ToolRegistry ──依赖──> Tool Protocol
                                      ▲
                         内置工具/MCP adapter 实现
```

高层 Agent 不再直接依赖六个具体函数。M6 接入 MCP 时，只需要把远端工具包装成
`Tool` 并注册。

## 4. JSON Schema 校验

模型输出的工具参数是不可信输入。即使 Schema 已经发送给模型，也不能假设模型一定
遵守它，必须在本地执行前再次校验。

当前使用 JSON Schema Draft 2020-12，注册时先校验 Schema 本身，调用时再校验参数：

- 缺少必填字段会得到 `invalid_arguments`；
- 字段类型错误不会进入 handler；
- `additionalProperties: false` 阻止模型偷偷传入未声明字段；
- 错误会带上字段路径和可读原因。

一个容易被追问的细节是：JSON Schema 中的 `default` 只是注解，标准校验器不会自动
修改输入。`glob` 和 `grep` 的可选 `path` 仍由 Python 函数默认值处理，不能把
Schema 的 `default` 当作参数填充器。

## 5. 结构化 ToolResult

工具现在返回：

```text
ToolResult
├── content       给模型理解的主要结果
├── is_error      本次工具调用是否失败
├── error_code    稳定的机器可读错误类别
├── duration_ms   Registry 测量的执行耗时
└── metadata      exit_code、截断数量、行数等结构化事实
```

主要错误类别包括：

- `invalid_arguments`：JSON 或 Schema 参数错误；
- `unknown_tool`：模型请求了未注册工具；
- `io_error`：文件系统操作失败；
- `timeout`：命令超过执行期限；
- `non_zero_exit`：命令已执行，但退出码不为零；
- `execution_error`：handler 抛出未预期异常或违反返回协议；
- `permission_denied`：用户拒绝操作。

`error_code` 应保持稳定，`content` 可以面向人和模型提供细节。调用方不应通过
`"Error" in content` 判断错误类型。

### 非零退出码为什么是错误，但仍要保留输出

测试失败通常返回非零退出码，但 stdout/stderr 正是 Agent 修复代码所需的信息。因此
`run_bash` 返回 `non_zero_exit`，同时把测试输出放在 `content`、退出码放在
`metadata`。错误分类不能以丢失诊断信息为代价。

### 截断为什么必须有元数据

只在字符串末尾写 `... truncated` 不足以让上层制定预算。现在结果会明确记录：

- 是否截断；
- 原始数量；
- 实际展示数量；
- exit code 或总行数等工具特有信息。

M4 可以据此决定进一步裁剪、摘要或重新读取更小范围。

## 6. Trace、日志与脱敏

每个 Agent 拥有独立的内存 trace。一次 trace 记录：

- call id 和工具名；
- 脱敏后的参数；
- 成功或错误类别；
- 耗时；
- 不包含工具正文的安全元数据。

REPL 的 `/trace` 用于查看这些记录。Trace 与普通日志的区别在于：日志主要服务人类
排障，trace 表达一次请求经过哪些结构化阶段，适合统计耗时、失败率和调用链。

脱敏采用两种机制：

1. 常见敏感键，如 `api_key`、`token`、`password`、`authorization`；
2. Schema 属性上的 `x-sensitive: true`，用于源码正文、替换文本和 shell 命令。

长字符串还会在 trace 中单独截断。工具结果正文不会写入 trace，避免把源码、测试
输出或秘密复制到可观测数据中。

脱敏不是万能的：如果秘密藏在一个未标记且名称普通的字段中，系统无法可靠理解其
语义。工具作者必须正确标记 Schema；M3 还需要限制日志访问和持久化范围。

## 7. 如何运行和体验 M2

安装新增依赖并运行测试：

```bash
cd /home/shaoran/workspace/sakicode
uv sync --extra dev
uv run pytest tests/test_tooling.py tests/test_tools.py -vv
```

查看注册表生成的模型工具列表：

```bash
uv run python -c \
  'from sakicode.tools import create_registry; print([s["function"]["name"] for s in create_registry().schemas()])'
```

应看到六个内置工具名。

### 实验一：成功调用与 trace

启动 SakiCode：

```bash
export DEEPSEEK_API_KEY="你的 API Key"
uv run sakicode
```

在 REPL 中输入：

```text
saki> 请读取 README.md 并概括项目用途
saki> /trace
```

应看到类似：

```text
Tool traces:
1. read_file [ok] 0.123 ms args={"path": "README.md"}
```

### 实验二：结构化非零退出码

输入：

```text
saki> 请运行命令 python -c "import sys; print('demo failure'); sys.exit(3)"，并解释结果
```

批准后，模型收到的 tool result 中应包含：

```json
{
  "ok": false,
  "error_code": "non_zero_exit",
  "metadata": {"exit_code": 3}
}
```

再执行 `/trace`，可以看到 `run_bash [error:non_zero_exit]`。命令参数会显示为
`[REDACTED]`，因为 shell 命令可能内嵌 token 或密码。

### 实验三：Schema 在 handler 前拒绝参数

这一行为不依赖模型，直接运行对应测试最稳定：

```bash
uv run pytest tests/test_tooling.py::test_registry_validates_arguments_before_invocation -vv
```

测试中的 handler 会设置一个标记；参数类型错误后断言该标记仍为 `False`，证明验证
发生在执行之前。

### 实验四：输出截断元数据

运行：

```bash
uv run pytest tests/test_tools.py::test_read_file_reports_truncation_metadata -vv
```

测试把最大展示行数临时缩小到 2，验证第三行没有进入 `content`，同时
`metadata` 保留 `truncated=true`、`shown_lines=2` 和 `total_lines=3`。

## 8. 建议阅读顺序

1. `src/sakicode/tooling.py`：协议、结果、Registry、trace 和脱敏；
2. `tests/test_tooling.py`：先理解协议层的不变量；
3. `src/sakicode/tools.py`：六个业务工具如何返回结构化结果；
4. `src/sakicode/agent.py::_execute_tool`：审批和 Registry 如何串联；
5. `src/sakicode/repl.py::format_traces`：trace 如何暴露给用户。

## 9. 你应该能回答的面试问题

1. 为什么 Agent 不应该直接依赖具体工具函数？
2. 为什么把 Schema 发给模型后，本地仍然必须再次校验？
3. `ToolResult` 为什么同时需要 `content`、`error_code` 和 `metadata`？
4. 非零退出码、超时和 Python 异常为什么要分成不同错误类别？
5. 为什么 trace 不应直接记录完整工具输入和输出？
6. `Protocol` 与抽象基类相比，在 MCP 工具接入中有什么优势？
7. JSON Schema 的 `default` 是否会自动填充缺失参数？

## 10. 动手练习

实现一个只读的 `list_directory` 工具：

- 参数包含 `path` 和可选的 `max_entries`；
- 使用 JSON Schema 限制 `max_entries` 为 1 到 500 的整数；
- 返回总条目数、展示条目数和是否截断；
- 注册后能出现在 `registry.schemas()`；
- 为成功、路径不存在、参数越界和截断分别写测试。

## 附录：面试问题参考答案

### 1. 为什么 Agent 不应该直接依赖具体工具函数？

直接依赖会把工具发现、参数格式、调用方式和错误处理散落在 Agent 中。引入 Registry
和 Tool 协议后，Agent 只依赖稳定抽象；内置函数、插件或 MCP 工具可以独立扩展，并
统一经过校验、权限、计时和 trace。这降低了耦合，也避免不同工具来源绕过安全策略。

### 2. 为什么把 Schema 发给模型后，本地仍然必须再次校验？

发给模型的 Schema 只是生成提示，不是安全边界。模型可能输出缺字段、错误类型、
额外字段或恶意参数，兼容服务也可能不严格执行 structured output。本地校验是执行前
的确定性守卫，保证 handler 只接收满足契约的输入。

### 3. ToolResult 为什么同时需要 content、error_code 和 metadata？

`content` 提供可读诊断，适合模型理解；`error_code` 是稳定的机器分支条件；
`metadata` 保存 exit code、行数和截断状态等结构化事实。只用字符串会迫使调用方解析
文案，只用错误码又会丢失修复所需的上下文。

### 4. 非零退出码、超时和 Python 异常为什么要分成不同错误类别？

它们的恢复策略不同。非零退出码往往是正常的测试失败，模型应阅读输出并修改代码；
超时可能需要缩小命令或提高限制；Python 异常通常表示工具实现缺陷或未覆盖情况。
准确分类使 Agent、重试器和指标系统能够采取不同动作。

### 5. 为什么 trace 不应直接记录完整工具输入和输出？

输入可能包含密钥、Cookie、源码和 shell 凭据，输出也可能包含源代码、用户数据或
巨量日志。完整记录会扩大泄露面和存储成本。Trace 应记录诊断所需的最小结构化信息，
对敏感字段脱敏、长值截断，并避免复制正文；需要详细内容时应通过受控的原始数据源
查询。

### 6. Protocol 与抽象基类相比，在 MCP 工具接入中有什么优势？

`Protocol` 支持结构化子类型，对象只要具有规定成员即可使用，无需继承项目内部基类。
因此 MCP adapter、测试替身和第三方工具都能以很薄的包装接入。抽象基类适合需要共享
实现或强制生命周期的场景；这里主要需要统一接口，所以 Protocol 更低耦合。

### 7. JSON Schema 的 default 是否会自动填充缺失参数？

不会。标准 JSON Schema 校验只判断实例是否有效，`default` 通常是注解。若要自动
填充，必须在校验前后实现明确的默认值应用逻辑，或由 Python handler 的默认参数处理。
自动填充还应发生在 trace 和权限判断之前，确保所有组件看到同一份规范化参数。
