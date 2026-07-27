# SakiCode 简历对齐升级与学习路线

目标不是把功能名写进仓库，而是让简历中的每句话都有三类证据：

1. 可解释的设计取舍；
2. 可运行的代码与自动化测试；
3. 可复现的演示或指标。

## 现状与差距

| 简历描述 | v1 现状 | 达标证据 |
| --- | --- | --- |
| Agent 执行状态机与仓库工具集 | 有隐式 `while` 循环和 6 个工具，无显式状态与事件 | 状态转换、异常/中断/预算终态、转换测试、运行轨迹 |
| Token Budget、压缩、检查点恢复 | 仅按字符数估算并告警 | token 计数、分层裁剪、摘要策略、原子化 checkpoint、恢复测试 |
| 细粒度权限与审批 | 3 个工具统一逐次 y/n | 风险分级、路径/命令作用域、allow/ask/deny、审批记录、绕过测试 |
| MCP 客户端与 Skill 系统 | 尚未实现 | stdio MCP、工具发现、JSON Schema 校验、超时/进程隔离、Skill 渐进加载与作用域 |

## 实施顺序

### M1：显式 Agent Runtime 状态机

状态：已完成第一版，配套讲义见
[`docs/learning/01-agent-runtime.md`](docs/learning/01-agent-runtime.md)。

状态包括 `idle`、`requesting_model`、`executing_tools`、
`waiting_approval`、`completed`、`failed`、`limit_reached` 和
`interrupted`。所有转换集中校验并记录原因。

验收：

- 模型 → 工具 → 模型 → 最终答复的轨迹可检查；
- 非法转换立即失败；
- API 错误、用户中断和工具预算分别进入不同终态；
- 单元测试不访问真实模型。

应掌握：

- 有限状态机的状态、事件、转换、守卫条件和终态；
- 为什么“控制流”不等于“显式状态机”；
- Tool Calling 中 assistant/tool 消息的配对约束；
- 如何用依赖替身测试 agent loop。

实践题：画出一次“编辑代码 → 测试失败 → 再编辑 → 测试通过”的状态轨迹，
并说明审批发生在哪条边上。

### M2：工具协议与可观测性

状态：已完成第一版，配套讲义见
[`docs/learning/02-tool-protocol-and-observability.md`](docs/learning/02-tool-protocol-and-observability.md)。

把工具从全局函数表重构为统一的 `Tool`/`ToolRegistry` 协议，加入结构化
`ToolResult`、错误类别、耗时和截断元数据。保留内置仓库工具，并为后续 MCP
工具提供同一调用入口。

验收：

- 工具注册、发现和参数 Schema 校验有测试；
- 超时、非零退出、输出截断不再混成普通字符串；
- 每轮可输出结构化 trace，敏感参数会脱敏。

应掌握：Protocol/依赖倒置、JSON Schema、结构化错误、日志与 trace 的区别。

### M3：细粒度权限与审批

状态：已完成第一版，配套讲义见
[`docs/learning/03-permissions-and-approval.md`](docs/learning/03-permissions-and-approval.md)。

引入 `allow / ask / deny` 决策，风险维度包含工具种类、读写行为、工作区路径、
命令特征和已授权作用域。审批只授权规范化后的精确目标或规则，不直接信任模型文本。

验收：

- 工作区外写入默认拒绝，高风险命令默认拒绝；
- 支持“仅本次”和“本会话同类操作”；
- 符号链接、`..`、shell 组合命令等绕过场景有安全测试；
- 审批决定可审计。

应掌握：最小权限、默认拒绝、路径规范化、shell 攻击面、TOCTOU。

### M4：分层上下文与 Token Budget

状态：已完成第一版，配套讲义见
[`docs/learning/04-layered-context-and-token-budget.md`](docs/learning/04-layered-context-and-token-budget.md)。

将上下文分成 system/instruction、任务状态、近期对话、工具结果四层。预算分配后，
先按策略裁剪大型工具输出，再把较老历史压缩成带事实/决策/待办的结构化摘要。

验收：

- 使用模型对应 tokenizer（未知模型使用保守估计）；
- 每层有预算和不可丢弃信息；
- 压缩前后 tool-call 配对始终合法；
- 用长对话 fixture 验证请求不超预算且关键事实保留。

应掌握：context window、输入/输出预算、recency 与 salience、摘要漂移、
prompt injection 在长期记忆中的传播风险。

### M5：检查点与长程任务恢复

保存版本化 checkpoint：消息、任务摘要、runtime 终态、预算使用、权限授予和工具
trace。采用临时文件 + 原子替换，恢复时校验版本与工作区身份；不保存 API Key。

验收：

- 进程退出后可用 session id 恢复；
- 半写文件不会成为有效 checkpoint；
- schema 迁移、损坏数据和敏感字段均有测试。

应掌握：序列化 schema、原子写、幂等性、崩溃一致性、secret hygiene。

### M6：MCP 客户端

先实现 stdio transport，再实现 initialize/list_tools/call_tool。远端工具转换到统一
registry，调用前做 Schema 校验，并在超时后终止隔离的子进程。

验收：

- 用本地假 MCP server 完成握手、发现和调用；
- 协议错误、超时、进程崩溃不会拖垮 agent；
- MCP 工具经过与内置工具相同的权限引擎。

应掌握：JSON-RPC、能力协商、stdio framing、生命周期、超时与故障隔离。

### M7：声明式 Skill 系统

定义 `SKILL.md` 元数据与目录约定。启动时只建立轻量索引，匹配任务后再加载正文和
所需资源；作用域分为内置、用户和项目，采用明确的覆盖规则。

验收：

- 支持检索、渐进加载、作用域优先级和冲突诊断；
- 非相关 Skill 正文不会进入 prompt；
- 路径越界与恶意元数据有测试。

应掌握：检索与路由、progressive disclosure、配置优先级、prompt 供应链安全。

### M8：端到端评测与简历证据

建立固定任务集，例如新增 CLI 参数、修复测试、跨文件重构和拒绝危险命令。记录
成功率、工具调用数、token 使用、耗时、审批次数和恢复成功率。

最终简历中的每个动词都应链接到对应模块/测试，每个性能或准确率数字都应能由评测
脚本复现。没有评测支撑的数字不写入简历。

## 推荐学习节奏

每个里程碑都按“先画设计 → 写失败测试 → 最小实现 → 故障注入 → 复盘讲解”推进。
完成后用五分钟回答三件事：解决了什么失败模式、核心不变量是什么、为什么没有选择
更复杂的方案。能够脱离代码回答，才算真正掌握。
