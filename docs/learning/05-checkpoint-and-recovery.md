# M5 学习讲义：检查点与长程任务恢复

## 1. 这次改造解决了什么

M4 让一次模型请求不会越过 context window，但 `Agent.messages`、运行状态、token
统计、审批记录仍然只存在于内存。终端关闭、程序升级或机器重启后，长任务只能从头
开始。若只保存对话而不保存授权和 trace，恢复后的 Agent 也会对“之前做过什么、
为什么获准、花了多少预算”失忆。

M5 把一次逻辑会话的稳定状态保存成版本化 JSON checkpoint：

```text
Agent 到达终态
     │
     ▼
构造 schema v2 快照
     ├─ messages + task summary
     ├─ runtime 终态与转换历史
     ├─ context 限额与 token usage
     ├─ session grants + approval audit
     └─ redacted tool traces
     │
     ▼
敏感信息脱敏 → Schema 校验
     │
     ▼
同目录临时文件 → flush → fsync(file)
     │
     ▼
os.replace(temp, session.json) → fsync(directory)
```

CLI 启动时生成并显示 session id。完成、失败、工具预算耗尽或用户中断后自动提交
checkpoint；新进程使用 `--resume <session-id>` 恢复。恢复只重建内存状态，绝不重放
历史工具调用。

## 2. Checkpoint 不只是“把对象 dump 成 JSON”

一个可靠 checkpoint 需要同时回答五个问题：

1. **保存什么**：哪些状态决定后续行为？
2. **何时保存**：当前状态是否满足协议不变量？
3. **如何提交**：进程在任意写入时刻崩溃会留下什么？
4. **如何演进**：旧版本如何被新代码理解？
5. **能否信任**：文件是否属于当前工作区，是否夹带 secret 或损坏数据？

缺少其中任何一项，`json.dump(agent.__dict__)` 都只是调试快照，不是持久化协议。
Python 对象里可能含 client、函数、锁等不可序列化资源；内部字段也会随重构变化，
直接 dump 会把实现细节意外变成永久 API。

因此 `CheckpointStore` 显式挑选字段，并由 JSON Schema 定义持久边界。`Agent` 可以
继续重构，只要写入和迁移仍产出当前 schema，旧会话就不必跟着内部类布局变化。

## 3. 当前 schema 保存了什么

schema v2 的顶层结构如下：

```text
checkpoint
├── schema_version
├── session_id
├── saved_at
├── workspace {root, identity}
├── agent
│   ├── model
│   ├── messages
│   ├── task_summary
│   ├── runtime {state, history}
│   └── budget
│       ├── limits
│       ├── last_prompt_tokens
│       ├── total_prompt_tokens
│       ├── total_completion_tokens
│       └── last_context_stats
├── permissions {session_grants, audit_log}
└── tool_traces
```

这些字段分别承担不同职责：

- `messages` 是后续推理的无损事实源，`task_summary` 是 M4 最近一次生成的有损视图；
- runtime 保存终态与转换原因，恢复后下一轮仍通过状态机合法开始；
- budget 同时保存配置限额与实际使用量，避免恢复后统计归零或预算语义漂移；
- permissions 保存由权限引擎产生的 capability 与审计证据；
- trace 保存工具结果类别、耗时、脱敏参数与 metadata，用于解释恢复前的行为。

没有保存 OpenAI client、Console、工具函数等进程资源。它们由新进程重新构造，再把
持久状态应用上去。这是状态（state）与资源（resource）的边界。

## 4. 为什么只提交终态

当前可提交 runtime 状态只有 `completed`、`failed`、`limit_reached` 和
`interrupted`。`requesting_model`、`executing_tools`、`waiting_approval` 都不直接
落成有效 checkpoint，因为这些状态含有未完成的外部交互：模型流可能只收到一半，
工具可能已经产生副作用但结果尚未记录，审批问题也可能还没有答案。保存后“从中间
继续”很容易重复执行不可幂等操作。

还要维护 Tool Calling 的原子组：

```text
assistant(tool_calls=[A, B])
tool(A)
tool(B)
```

若用户在 `tool(A)` 后中断，内存里暂时存在半个消息组。M5 写入前通过
`_stable_messages()` 取最长协议完整前缀：保留本轮 user 请求，丢掉未闭合的
assistant/tool 后缀。恢复后模型可以重新规划，但不会接收到 API 必然拒绝的孤立
tool result。

该方案保证“恢复到最近稳定边界”，不是任意指令级续跑。若未来希望在一个超长 turn
内逐工具恢复，就必须额外设计 operation id、工具幂等键和副作用提交日志；仅增加
保存频率不能解决重复执行问题。

## 5. 原子写与崩溃一致性

直接覆盖目标文件有一个致命窗口：

```text
open(session.json, "w")  # 原文件先被截成 0 字节
write(first_half)
<进程崩溃>
```

此时旧 checkpoint 已丢失，新 checkpoint 又只有半份。M5 使用同目录临时文件和原子
替换：

1. `mkstemp()` 在目标目录创建唯一临时文件；
2. 写完 JSON 后 `flush()` 把用户态缓冲交给操作系统；
3. `fsync(file)` 要求文件内容进入持久化边界；
4. `os.replace(temp, target)` 原子切换文件名；
5. `fsync(directory)` 持久化目录项变化；
6. 失败时清理临时文件，旧目标保持不变。

“同目录”很关键：跨文件系统 rename 通常不能保证原子性，甚至会直接失败。
`os.replace` 的可见性保证是读者要么看到旧文件，要么看到完整新文件，不会看到中间
内容；`fsync` 关注断电后的持久性。两者解决的不是同一个问题。

测试通过故障注入让 `os.replace` 在最后一步失败，验证旧文件逐字节保持不变、临时
文件被清理且旧 checkpoint 仍可加载。另一个测试留下故意截断的 `.tmp` 文件；loader
只按精确的 `<session-id>.json` 查找，因此半写临时文件不会成为有效会话。

## 6. Schema 版本与迁移

schema version 是持久化数据的协议版本，不等于 Python package 版本。当前 loader 的
流程是：

```text
解析 JSON
  → 检查敏感数据
  → 读取 schema_version
  → 旧版本迁移到当前结构
  → 按当前 JSON Schema 校验
  → 校验 session id 与 workspace identity
  → 返回可应用状态
```

M5 提供 v1 到 v2 的迁移示例：v1 的扁平字段被重组到 `agent`、`permissions` 和
`workspace` 命名空间。迁移函数是纯转换：load 不修改源文件；只有下一次正常保存才
通过原子替换把 v2 持久化。这样“尝试读取”不会破坏唯一的旧副本，迁移失败也可重复
诊断。

未知版本明确抛出 `UnsupportedCheckpointVersion`，不能猜测字段含义。实际项目有多
个历史版本时，应采用连续迁移：`v1 -> v2 -> v3`，每一步小而可测，而不是为每个旧
版本维护一条直达当前版本的组合爆炸路径。

## 7. 校验损坏数据

JSON 能解析不代表数据可用。下面都是语法合法、语义损坏的 checkpoint：

- `runtime.state = "executing_tools"`，不满足可提交终态；
- `messages` 被写成字符串；
- token 使用量为负数；
- session id 与文件名不同；
- 缺少权限或 trace 字段。

`Draft202012Validator` 在构造任何运行时对象之前检查结构。错误包含字段位置，例如
`agent.runtime.state`，使用户能区分“文件不存在”“JSON 截断”“schema 不合法”和
“版本不支持”。验证必须位于反序列化边界，而不能等某个深层属性访问偶然报
`KeyError`。

JSON 也比 pickle 更适合这个边界。加载不可信 pickle 可能执行任意代码；JSON 只产生
基础数据结构，再经过 schema 白名单验证。可读性和跨版本迁移也更直接。

## 8. 工作区身份绑定

session id 只标识会话，不能证明会话属于当前仓库。两个项目可能恰好复制或共享同名
checkpoint；直接恢复会把项目 A 的消息和 `workspace-write` 授权带入项目 B。

M5 保存规范化绝对路径，并对其计算 SHA-256 identity。恢复时用当前
`Path.cwd().resolve()` 重算并比较，不一致就抛出 `WorkspaceMismatchError`。这不是
密码学认证——本地文件可被有权限的人修改——而是防止误用和跨工作区 capability
漂移。

生产系统可使用仓库内随机 workspace UUID、设备密钥签名或项目配置中的稳定 ID。
当前“规范化路径哈希”的取舍是不创建额外身份文件、行为确定且易解释；代价是仓库
整体移动后旧 checkpoint 需要显式迁移，不能静默恢复。

## 9. Secret hygiene

Agent 本身不保存 API client，因此配置中的 API Key 不会自然进入 schema。但 secret
仍可能出现在用户消息、工具参数、工具输出或扩展 metadata 中。M5 使用两层防线：

1. 保存前递归处理常见敏感键（`api_key`、`password`、`secret`、`token` 等）和常见
   `sk-...` / 环境变量赋值形式，替换成 `[REDACTED]`；
2. 加载外部或手工修改的 checkpoint 时再次扫描，发现未脱敏值就拒绝恢复。

工具 trace 在 M2 已按 schema 和键名脱敏，checkpoint 层再次处理属于纵深防御。
敏感信息识别不可能完备：自定义凭据格式、编码后的 secret 或普通文本中的私钥块还
需要专门规则。生产实现应加入 provider-specific detector，并考虑对整个 checkpoint
静态加密；加密不能替代最小化保存，因为解密后的数据仍会暴露给进程。

## 10. 恢复权限为什么不等于“相信摘要”

M4 强调不能从任务摘要恢复授权；M5 又明确保存 `session_grants`。两者并不矛盾：

- 摘要是模型可见的有损、不可信文本，“用户之前同意了”只是一句话；
- session grant 是 `PermissionEngine` 产生的类型化 capability，来自审批记录；
- capability 只从 schema 合法且 workspace identity 匹配的本地 checkpoint 恢复；
- 恢复过程不解析消息内容来创造新授权。

权限状态的事实源仍是权限引擎，而不是自然语言。审计日志一同恢复，使用户能用
`/approvals` 查看授权来历。若产品把“session”严格定义为单个 OS 进程，则不应恢复
grant；本项目把 session id 定义为可跨进程延续的逻辑会话，所以恢复是刻意的产品
语义，也是 roadmap 的验收要求。

## 11. 幂等性与副作用重放

恢复时只赋值 messages、runtime、计数器、grant、audit 和 trace。它不会调用模型，
不会执行 registry 中任何工具，也不会重放审批。否则历史中的 `write_file`、数据库
写入或外部 API 调用可能发生第二次。

幂等操作满足重复执行与执行一次效果相同，例如“把完整文件内容设为 X”在目标未被
其他人修改时近似幂等；“追加一行”“发送邮件”“扣款”通常不幂等。Checkpoint 自身
按 session id 覆盖，是幂等风格的状态提交；工具副作用不能假定幂等，所以恢复边界
必须避开“已执行但未记录结果”的模糊状态。

更强的长程执行器常使用 write-ahead log：先记录 operation id 与意图，执行外部操作，
再记录结果；外部系统按 operation id 去重。那是从“会话 checkpoint”走向“可恢复
工作流引擎”的下一层设计。

## 12. 如何运行与验证

启动新会话：

```bash
cd /home/shaoran/workspace/sakicode
uv run sakicode
```

启动信息会显示类似：

```text
Session: 4e352cfdbbea44e886386f639b78004b
```

至少完成一轮后退出，再恢复：

```bash
uv run sakicode --resume 4e352cfdbbea44e886386f639b78004b
```

checkpoint 默认位于 `.sakicode/checkpoints/<session-id>.json`，目录已加入
`.gitignore`。不要手工复制 checkpoint 到另一个工作区；身份校验会拒绝恢复。

运行 M5 专项测试：

```bash
UV_CACHE_DIR=/tmp/sakicode-uv-cache uv run pytest tests/test_checkpoint.py -vv
```

关键测试与验收标准对应关系：

- `test_session_round_trip_restores_all_long_lived_state`：跨进程语义和完整状态恢复；
- `test_atomic_replace_failure_preserves_previous_checkpoint`：替换前崩溃不破坏旧版本；
- `test_half_written_temp_file_is_not_a_checkpoint`：半写文件不可见；
- `test_v1_checkpoint_is_migrated_in_memory`：schema 迁移且 load 不覆写源文件；
- `test_unknown_schema_and_corrupt_data_are_rejected`：未知版本与损坏数据；
- `test_secrets_are_redacted_on_save_and_rejected_on_load`：双向 secret hygiene；
- `test_checkpoint_is_bound_to_the_workspace_identity`：跨工作区拒绝；
- `test_interrupted_half_tool_bundle_is_trimmed_to_a_valid_prefix`：中断消息协议完整。

测试全部使用本地假状态，不访问真实模型。

## 13. 建议阅读顺序

1. `checkpoint.py::CheckpointStore.save()`：原子提交路径；
2. `_SCHEMA_V2`：持久化边界与必需字段；
3. `CheckpointStore.load()`：验证、迁移和身份检查顺序；
4. `_stable_messages()`：中断后的 Tool Calling 不变量；
5. `_redact_secrets()` / `_contains_secret()`：写前与读后两层防线；
6. `Agent.save_checkpoint()` / `restore_checkpoint()`：状态与进程资源如何分离；
7. `runtime.py`、`permissions.py`、`tooling.py` 中的 snapshot/restore 接口；
8. `tests/test_checkpoint.py`：用故障注入理解每项保证。

## 14. 你应该能回答的面试问题

1. 为什么直接覆盖 JSON 文件不满足崩溃一致性？
2. `flush`、`fsync(file)`、`os.replace` 和 `fsync(directory)` 分别解决什么？
3. 为什么临时文件必须与目标文件位于同一目录/文件系统？
4. checkpoint 为什么只接受 runtime 终态？
5. 中断发生在多个 tool result 中间时，如何维持消息配对？
6. 为什么恢复不能重放工具调用？哪些工具不是幂等的？
7. schema version 与项目版本有什么区别？
8. 为什么迁移应先在内存完成，而不是 load 时原地改旧文件？
9. JSON 已能解析，为什么还需要 JSON Schema？
10. 为什么不用 pickle 保存完整 Agent？
11. workspace identity 防御的是什么？它还不能防御什么？
12. 为什么可以恢复类型化 session grant，却不能从摘要推断授权？
13. “不把 API Key 作为字段保存”为什么仍不足以保证 secret hygiene？
14. 原子 checkpoint 与 exactly-once 工具执行之间还缺什么机制？

## 15. 动手练习

### 练习一：设计 schema v3

给每条消息增加稳定 `message_id`，给摘要增加 `source_message_ids`。写出 v2 到 v3 的
迁移，并回答：旧消息没有 id 时，如何生成确定性且不会每次 load 都变化的 id？

### 练习二：故障点枚举

在 save 流程的六个步骤之间逐一假设断电，画出“目标文件、临时文件、目录项”可能的
状态，并说明下次 load 应看到旧版本、新版本还是明确失败。

### 练习三：幂等工具

为一个“追加日志”工具设计 operation id 去重。要求进程在“外部写入成功、checkpoint
尚未提交”时崩溃，恢复后重试也不能追加两次。说明去重记录应该由 Agent 保存还是由
外部资源与写入一起原子提交。

### 练习四：强化 secret detector

增加 PEM 私钥头、GitHub token 和 bearer authorization 的检测 fixture。比较“直接
拒绝保存”和“自动脱敏后保存”的可恢复性、可观察性与误报代价。
