# M3 学习讲义：细粒度权限与审批

## 1. 为什么不能继续使用统一 y/n

M2 之前，三个写工具共用一个布尔标记 `requires_confirmation`，审批问题永远是
一句 `Allow? [y/N]`。这个设计有四个根本缺陷：

- 不区分风险：改写一行源码和 `rm -rf ~` 得到同一个问题；
- 不可组合：用户无法表达「这类操作本会话都允许」，只能在每一次打断中重复
  回答；
- 不可信目标：提示里展示的是模型生成的原始参数文本，用户批准的可能不是
  实际被执行的目标；
- 不可审计：拒绝了什么、批准了什么、为什么，事后都无从查证。

统一 y/n 的本质问题是把「判断风险」这件系统应该做的事推给了用户。人会在第
二十次弹窗时无脑按 y，审批就失效了。M3 的目标是把决策权收回代码：

```text
allow ── 策略能证明安全（如工作区内只读），直接执行
ask   ── 策略无法证明安全，但用户可以决定（带规范化目标）
deny  ── 策略证明危险（如工作区外写入），问都不问
```

关键转变：用户不再是唯一防线，而是三层决策中的最后一层。

## 2. 当前调用链

M2 图中「权限确认」那一格，现在展开成完整的引擎流程：

```text
模型返回 tool call
        │
        ▼
Agent 解析 JSON ──失败──> ToolResult(invalid_arguments)
        │
        ▼
ToolRegistry JSON Schema 校验（M2）
        │
        ▼
PermissionEngine.evaluate(name, args)
        │
        ├─ 未注册的未知工具 ─────────> DENY（默认拒绝）
        ├─ 写工作区外 / 高风险命令 ──> DENY ──> ToolResult(permission_denied)
        ├─ grant_key 命中会话授权 ───> ALLOW（审计记 session_grant_hit）
        ├─ 只读工具且在工作区内 ─────> ALLOW（审计记 policy_allow）
        └─ 其他 ───────────────────> ASK
                                        │
                                        ▼
                          runtime 进入 WAITING_APPROVAL
                          提示展示规范化 target（非模型原文）
                [y] 仅本次 / [s] 本会话同类（可授权时）/ [N] 拒绝
                                        │
                                        ▼
                    用户选择经 engine.record() 写入审计日志
        │
        ▼
ToolRegistry 查找工具 → Tool.invoke()（M2）
        │
        ▼
ToolResult → ToolTrace → JSON tool message 返回给模型
```

注意两个边界：Schema 校验在权限评估之前（畸形参数不消耗一次审批）；DENY
不经过 `WAITING_APPROVAL` 状态（策略已决定的事不打扰用户）。

## 3. Decision、PolicyDecision 与授权键

每次评估产出一个不可变的 `PolicyDecision`：

```text
PolicyDecision
├── decision            allow / ask / deny
├── reason              面向人和审计的原因
├── target              规范化目标：resolve 后的绝对路径或压缩空白的命令
├── grant_key           会话授权的稳定键（仅由规范化数据构造）
└── session_grantable   是否允许「本会话同类」授权
```

授权键的设计是 M3 最核心的取舍，按工具类别分两种粒度：

- **类级键**：写操作的会话授权键是 `workspace-write`，读工作区外是
  `read-outside`。一次「本会话同类」覆盖该类所有操作。理由是：文件级的
  精确授权会形成事实上的逐文件弹窗，而目录级授权又引入「哪个目录」的
  歧义；工作区本身就是这个项目声明的信任边界，类级键语义最可预测。
- **精确键**：bash 的会话授权键是 `bash:<压缩空白后的命令文本>`。「同类
  操作」对 shell 而言只能是同一条命令——`git status` 和 `git push` 绝不
  应共享授权。

「仅本次」授权不写入任何键，因此天然不持久：同一操作第二次仍然 ASK。
`approve_session()` 会忽略 `session_grantable=False` 的决策，一次性批准
永远不可能悄悄变成常驻权限。

## 4. 路径规范化与 `..`/符号链接攻击

模型给出的路径是不可信输入。两种经典绕过：

```text
write_file(path="subdir/../../outside/secret.txt")   # .. 逃逸
write_file(path="link.txt")   # link.txt 是指向工作区外的符号链接
```

如果只拼接字符串再检查前缀，`workspace/subdir/../../outside/secret.txt`
会以工作区前缀开头而误判为安全。引擎的做法（`_resolve`）：

1. 相对路径先锚定到 `workspace_root`（而不是进程 cwd，避免语义漂移）；
2. `Path.resolve()` 折叠 `..` 并跟随符号链接，得到真实落点；
3. 用 `is_relative_to(workspace_root)` 判断真实落点是否在工作区内。

因此符号链接写入会被判定为工作区外写入而 DENY——判断的是「最终写到哪里」，
而不是「路径长什么样」。对应测试见
`tests/test_permissions.py::test_symlink_pointing_outside_workspace_is_denied`
和 `test_dotdot_escape_is_denied`。

风险维度的不对称也在这里体现：写工作区外直接 DENY（不可逆、可能是系统
文件），读工作区外只是 ASK（可逆、经常合法，如读日志和系统头文件），且
读授权是类级 `read-outside`。

## 5. Shell 攻击面：高风险模式与组合命令

`run_bash` 是最危险的工具，引擎分三层处理：

**第一层：高风险模式直接 DENY。** 一个保守的正则列表拦截 `rm -rf /`、
`sudo`、`dd of=/dev/*`、`mkfs`、fork bomb、`shutdown`/`reboot`、
`curl|wget ... | sh` 这类管道执行。必须诚实说明：这是启发式，永远不可能
完备——混淆、编码、变量展开都能绕过正则。它的定位是「拦截无歧义的灾难」，
不是「证明命令安全」；未匹配的命令仍然要 ASK。高风险模式在**完整原始
文本**上匹配，所以 `ls && sudo ...` 里的 sudo 也逃不掉。

**第二层：组合命令永远逐次问。** 含有 `&&`、`;`、`|`、`>`、`<`、`$()`、
反引号、`||` 或换行的命令，`session_grantable=False`。原因很简单：如果
允许会话授权 `ls`，授权键按命令文本匹配，那么 `ls && rm -rf build` 是一
条新命令、不会命中授权——看起来安全。但反过来想，任何「按命令前缀/程序
名」的宽松匹配都会立刻被组合命令攻破（批准 `ls` 变成批准
`ls && anything`）。既然无法安全地定义「同类组合命令」，就干脆不给组合
命令会话授权。`approve_session()` 对这类决策是空操作，测试
`test_composite_command_cannot_become_a_session_grant` 固定了这一行为。

**第三层：简单命令按精确文本授权。** 压缩空白后作为 `bash:<command>` 键，
同一命令本会话不再问，不同命令仍要问。

## 6. 最小权限、默认拒绝与 TOCTOU

三个安全原则的落地方式：

- **最小权限**：只读工具默认放行范围只有工作区；写工具必须逐次或按类
  授权；bash 授权精确到单条命令。任何组件拿到的权限都不超过完成任务
  所需。
- **默认拒绝**：不在分类表里的工具一律 DENY。这是为 M6 的 MCP 工具预留
  的防线——新工具接入时，忘记写策略的后果是「不能用」而不是「随便用」，
  失败方向是安全的。
- **TOCTOU（time-of-check to time-of-use）**：这是必须诚实面对的残留风险。
  路径在**审批时** resolve，文件在**执行时**才被打开，两者之间存在窗口：
  攻击者（或一个被模型污染的中间步骤）可以在审批后把工作区内的普通路径
  换成指向工作区外的符号链接，审批时的判断就过期了。

本项目的缓解（不是消除）：执行窗口极短（审批返回后立即 invoke）；工作区
外写入一律 DENY 使攻击者需要先把链接种进工作区内；高风险命令直接拒绝。
生产系统还能更进一步：用 `openat2(RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS)`
让内核在打开时原子地完成约束检查；或者以文件描述符传递已验证的文件
（检查即持有），从根本上消除 check 与 use 的分离。面试中被追问 TOCTOU
时，能说出「resolve 在审批时、执行在其后」这个窗口本身就值一半分数。

另一个已知的一致性问题：权限引擎把相对路径锚定到 `workspace_root`，而内
置工具实际执行时用的是进程 cwd。当前 CLI 启动时 `workspace_root` 默认就
是 `Path.cwd()`，两者一致；但如果未来允许指定其他工作区，必须让工具执行
也锚定到同一根目录，否则审批和执行的视图会分叉。

## 7. 审计：ApprovalRecord 与 /approvals

每一次评估的最终处置都会追加一条 `ApprovalRecord`：

```text
ApprovalRecord
├── tool        工具名
├── target      规范化目标（审计看到的和用户批准的是同一个东西）
├── outcome     policy_allow / policy_deny / allow_once /
│               allow_session / session_grant_hit / deny
├── reason      决策原因
└── timestamp   时间戳
```

审计记录只存规范化 target 而不存模型原始参数：审计要回答的问题是「系统
实际批准/拒绝了什么」，而不是「模型试图做什么」。outcome 枚举能区分
「策略放行」和「命中会话授权」——后者对复盘「这次会话我到底放权了多少」
至关重要。

REPL 新增 `/approvals` 命令，先列出当前生效的会话授权，再按序打印审计
日志。它回答两个高频问题：我现在处于什么授权状态？刚才那一串操作谁批的、
谁拒的？

## 8. 如何运行和体验 M3

运行全部权限测试：

```bash
cd /home/shaoran/workspace/sakicode
uv sync --extra dev
uv run pytest tests/test_permissions.py -vv
```

### 实验一：策略拒绝不打扰用户

启动 `uv run sakicode`，输入：

```text
saki> 请把 hello 写入 /tmp/m3-demo.txt
```

应直接看到工具结果为 `permission_denied`（content 中带 policy 原因），
全程没有审批弹窗。随后：

```text
saki> /approvals
```

审计里应有一条 `policy_deny`，target 是 `/tmp/m3-demo.txt`。

### 实验二：本会话同类授权

```text
saki> 请新建 notes/todo.md，写入三条待办
```

审批提示展示规范化后的绝对路径，选项是
`[y] once / [s] this kind for the session / [N] deny`。输入 `s` 后再让
模型改另一个文件：

```text
saki> 再在 notes/ideas.md 里写一条想法
```

第二次不再弹窗，控制台以 dim 样式打印命中了 `workspace-write` 授权。
`/approvals` 的 Session grants 一行列出 `workspace-write`，审计里出现
`allow_session` 和 `session_grant_hit` 两条记录。

### 实验三：组合命令不能会话授权

```text
saki> 请运行 ls && pwd
```

审批提示只有 `[y] once / [N] deny`，没有 `[s]` 选项——组合命令永远逐次
确认。

### 实验四：高风险命令

```text
saki> 请运行 sudo apt install htop
```

直接拒绝、无弹窗，`/approvals` 中新增一条 `policy_deny`，reason 指出
命中了 sudo 模式。

### 实验五：绕过测试

```bash
uv run pytest tests/test_permissions.py -k "symlink or dotdot or composite or high_risk" -vv
```

四条安全性质（符号链接、`..`、组合命令、高风险模式）各有独立测试。

## 9. 建议阅读顺序

1. `src/sakicode/permissions.py`：Decision、PolicyDecision、分类规则、
   授权键、审计；
2. `tests/test_permissions.py`：先用测试建立对规则的精确预期；
3. `src/sakicode/agent.py::_execute_tool`：引擎如何接入工具循环，DENY 与
   ASK 的状态转换差异；
4. `src/sakicode/agent.py::_request_approval`：规范化 target 如何展示、
   非 TTY 为什么自动拒绝；
5. `src/sakicode/repl.py::format_approvals`：审计如何暴露给用户。

## 10. 你应该能回答的面试问题

1. 为什么权限决策必须基于规范化后的目标，而不是模型给出的原始参数？
2. 为什么工作区外写入直接 DENY，而工作区外读取只是 ASK？
3. 为什么组合 shell 命令不能获得会话授权？
4. 授权键为什么对写操作是类级 `workspace-write`，对 bash 却是精确命令
   文本？
5. resolve 之后、执行之前还存在什么竞态窗口？这个项目如何缓解，生产
   系统还能怎么做？
6. 为什么未知工具要默认拒绝？这和 MCP 工具接入有什么关系？
7. 用正则列表拦截高风险命令，能保证 shell 安全吗？如果不能，它的定位
   是什么？

## 11. 动手练习

为引擎接入一个新的 `delete_file` 工具策略：

- 工作区内删除 → ASK，会话授权键为独立的类级键 `workspace-delete`（与
  `workspace-write` 分开，体会「删除比修改风险更高」的分级）；
- 工作区外删除 → DENY；
- 为符号链接目标、`..` 逃逸分别写测试；
- 在 REPL 中用 `/approvals` 验证 `workspace-delete` 授权独立于
  `workspace-write` 出现。

进阶：把 `workspace-write` 细化成目录级授权（如
`workspace-write:notes/`），思考为什么讲义说这会引入歧义——目录边界、
嵌套目录、`.` 分别该怎么算？把你的结论写在测试里。

## 附录：面试问题参考答案

### 1. 为什么权限决策必须基于规范化后的目标，而不是模型给出的原始参数？

模型输出是不可信输入，可能因幻觉、注入或对抗 prompt 而具有欺骗性。
`subdir/../../etc/passwd` 这样的路径按字符串看在工作区内，按真实落点看
在外面。只有 resolve 之后的路径和压缩空白后的命令才对应系统实际要操作
的对象，审批、授权键和审计都必须建立在这份规范化数据上，否则用户批准的
和执行的是两个东西。

### 2. 为什么工作区外写入直接 DENY，而工作区外读取只是 ASK？

风险不对称。写入不可逆且可能损坏系统文件或凭证，策略能证明它危险，所以
问都不问——多问一次只会训练用户无脑批准。读取可逆且经常合法（日志、
文档、示例代码），策略无法证明它危险，就把决定权交给用户，并用类级授权
避免重复打扰。这体现了「默认拒绝」不是一刀切：deny 给能证明危险的，
ask 给不能证明安全的，allow 给能证明安全的。

### 3. 为什么组合 shell 命令不能获得会话授权？

因为无法为组合命令安全地定义「同类」。精确文本匹配下，`ls && rm -rf x`
与 `ls` 是不同键，授权不泄露；但任何更宽松的匹配（按首命令、按程序名）
都会让「批准 `ls`」退化成「批准 `ls && 任意命令`」。既然找不到既宽松到
有用、又严格到安全的粒度，就让组合命令永远逐次确认。`session_grantable=
False` 加上 `approve_session` 对不可授权决策的空操作，保证一次性批准不
可能变成常驻权限。

### 4. 授权键为什么对写操作是类级 workspace-write，对 bash 却是精确命令文本？

文件写操作有天然的信任边界——工作区，类级键语义清晰且避免逐文件弹窗；
文件级精确键会形成事实上的每次打扰，目录级键又引入边界歧义。shell 命令
没有等价的安全边界：`git status` 和 `git push` 风险天差地别，唯一安全
的「同类」定义就是同一条规范化命令文本。键的粒度取决于该操作类别是否
存在一个可验证的安全边界。

### 5. resolve 之后、执行之前还存在什么竞态窗口？这个项目如何缓解，生产系统还能怎么做？

TOCTOU 窗口：审批时 resolve 得到安全结论，执行时才真正打开文件，期间
路径可能被替换成指向别处的符号链接，使审批结论过期。本项目的缓解是缩短
窗口（评估后立即执行）、工作区外写入一律 DENY、高风险命令一律拒绝。
生产系统可以让检查和操作原子化：`openat2` 的 `RESOLVE_BENEATH`/
`RESOLVE_NO_SYMLINKS` 让内核在 open 时强制约束，或者先打开文件拿到描述
符、对描述符做校验和操作（检查即持有），从机制上消除 check 与 use 的
分离。

### 6. 为什么未知工具要默认拒绝？这和 MCP 工具接入有什么关系？

权限分类表不可能预知所有工具。如果未知工具默认放行，那么 M6 接入 MCP
后，任何第三方 server 提供的工具都会绕过整个权限体系——供应链攻击的
直达通道。默认拒绝让「忘记写策略」的失败方向是功能不可用而非安全失守，
新增工具时必须显式地做出风险分级决策。这是默认拒绝原则在可扩展系统里
最重要的应用：扩展点必须是显式的。

### 7. 用正则列表拦截高风险命令，能保证 shell 安全吗？如果不能，它的定位是什么？

不能。变量展开、编码、路径等价写法、命令替换都能绕过模式匹配，任何静态
列表都不可能完备。它的定位是纵深防御中的一层：拦截无歧义的灾难模式
（`rm -rf /`、`dd of=/dev/sda`、fork bomb），把「明显不该发生」的操作
从用户审批负担中移除。真正的安全不来自这一层，而来自后面的两层：未匹配
命令仍需 ASK，组合命令不可会话授权。把启发式当成过滤器而不是证明器，
是安全工程的基本心态。
