# M8 学习讲义：端到端评测与可复现证据

## 1. 这次改造解决了什么

M1–M7 把能力逐个建了起来：状态机、工具协议、权限、上下文、检查点、MCP、
Skill。但"这个 Agent 真的能干活吗"一直没有量化回答。简历上写"支持任务集
回放和 Harness 对比评估"，需要的是**可复现的证据链**：固定任务集、自动评分、
指标落盘、两次运行可对比。没有评测支撑的数字不写进简历——这是 ROADMAP 一开
始定下的原则，M8 就是把这条原则变成代码。

M8 的整体结构：

```text
evals/tasks/<name>/
  workspace/   fixture 仓库（一个微型 Python 项目）
  task.json    prompt + approval 策略 + 声明式 checks
       │
       ▼  run_task()：复制 fixture 到临时目录，chdir 进入
Agent（真实工具注册表 + 权限引擎 + 检查点存储）
       │  approval_handler 按任务配置自动应答（不读 stdin）
       ▼
评分（checks 逐条执行）+ 指标收集 + 恢复验证
       ▼
evals/results/<run-id>.json        （指标与逐条 check 结果）
evals/results/transcripts/<run-id>/（完整轨迹：messages/traces/audit）
       ▼
--compare A B：按任务对齐，输出成功率、token、耗时、工具调用数的 delta
```

## 2. 设计取舍

**为什么 checks 是声明式的而不是每任务一个评分脚本。**
四类任务（修测试、加 CLI 参数、跨文件重构、拒绝危险命令）的评分都可以分解
成少数几种原语：跑一个命令看退出码、文件存在/包含某模式、审计日志里有没有
某条记录。把原语做成 `task.json` 里的数据，任务定义就不用写代码，新增任务
只是加一个目录。未知 check 类型判失败（fail closed），不会因为配置笔误误判
成通过。

**为什么用 `os.chdir` 而不是给所有工具注入工作区参数。**
内置工具、权限引擎、检查点存储本来就全部锚定进程 cwd（这是 M1 以来的既有设
计）。评测时 chdir 进 fixture 副本，整套机制零改动就能获得隔离；评分命令用
显式 `cwd=` 参数执行，不依赖进程 cwd。代价是评测不能并发跑任务——对一个小
任务集这不是问题，引入 per-task 工作区参数才是过度设计。

**为什么"拒绝危险命令"的成功判据是"没执行"而不是"拒绝了"。**
模型自己拒绝（不调用工具）和引擎 DENY（调用了但被拦下）都是守住护栏，正确
的不变量只有一条：**危险命令没有以"被批准"的身份出现在审计日志里**。
trace 里 run_bash 的参数因 `x-sensitive` 被脱敏，所以证据取审计日志的
normalized target——任何 outcome 属于批准类（allow_once / session_grant_hit
等）且匹配危险模式的记录都判失败。

**审批回调注入（`approval_handler`）是本次对既有代码的唯一改动。**
交互模式下 `_request_approval` 读 stdin；评测是非交互的，需要按任务配置
（allow/deny）自动应答。把"如何回答 ASK"抽成一个可注入的回调，handler 看到
的是与终端用户完全相同的规范化 `PolicyDecision`，权限引擎本身不变。

## 3. 指标定义（与简历逐条对应）

| 指标 | 来源 |
| --- | --- |
| 成功率 | 全部 checks 通过且 runtime 终态为 `completed` |
| 工具调用数 | `ToolRegistry.traces` 总数与按工具分桶 |
| token 使用 | `total_prompt_tokens` / `total_completion_tokens`（流式 usage 累加） |
| 耗时 | `run_turn` 墙钟时间 |
| 审批次数 | 权限审计日志按 outcome 计数 |
| 恢复成功率 | 任务结束后用同一 `CheckpointStore.load()` 重新加载并校验消息数 |

每次运行产出一份自描述的 JSON 报告（run id、model、逐任务结果、聚合指标），
轨迹单独存盘。报告文件就是"回放"的单位：复跑同一任务集得到新报告，
`--compare` 按任务名对齐输出 delta，这就是 Harness 对比评估。

## 4. 局限与下一步

- 单轮任务、串行执行、无并发；任务集只有 4 个，覆盖的是能力面而非难度梯度。
- 成功率对模型采样敏感，严格对比应多次运行取分布；当前报告记录单次结果。
- 评分命令在宿主环境执行（与 run_bash 同级风险），任务集必须可信。
