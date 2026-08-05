# M9 学习讲义：沙箱——把已批准命令的破坏半径关进工作区

## 1. 这次改造解决了什么

M3 的权限引擎回答的是"这条命令**能不能**执行"：allow/ask/deny、路径规范化、
高风险命令默认拒绝。但它有一个结构性盲区：**任何被批准的 `run_bash` 命令仍以
用户的完整权限执行**。模型被 prompt injection 诱导输出一条看似无害的命令、用
户对一连串审批产生疲劳误点 y、或者命令本身有意料之外的副作用——只要过了审批，
工作区之外的一切（$HOME、~/.ssh、网络）都对它敞开。

沙箱回答另一个问题："已批准的命令**能够到**什么"。两道防线是纵深关系：
权限引擎做调用前分类，沙箱做执行时 containment——即使分类错了或审批错了，
破坏也被限制在工作区内。

## 2. 为什么选 bubblewrap

候选方案与取舍：

| 方案 | 问题 |
| --- | --- |
| Landlock + seccomp 直写 | 无外部依赖但 API 底层（syscall/ctypes），策略难以一眼审阅，单测成本高 |
| Docker | 守护进程 + root 权限组 + 镜像管理，对一个 CLI 是数量级的重量 |
| firejail / nsjail | 同样是外部二进制，但 profile 体系复杂，攻击面更大（setuid） |
| **bubblewrap** | 单个用户态二进制，非 setuid（靠 unprivileged user namespaces），策略就是一段 argv——可读、可测、无需 root |

bwrap 的"策略即 argv"性质对本项目尤其合适：`build_argv()` 是纯函数，
单测不需要真的执行沙箱；集成测试则用 `skipif(bwrap_available())` 在支持的
机器上验证真实隔离。代价是 Linux-only，其他平台在 `auto` 模式下明确告警降级。

## 3. 沙箱策略（`src/sakicode/sandbox.py`）

每条批准的命令被包成：

```text
bwrap --ro-bind / / --tmpfs /tmp --bind <workspace> <workspace>
      [--tmpfs ~/.ssh ...] --dev /dev --proc /proc
      --unshare-net --unshare-pid --new-session --die-with-parent
      --chdir <cwd> -- bash -c <command>
```

四个设计细节：

1. **挂载顺序即语义**。bwrap 按顺序应用挂载：`--tmpfs /tmp` 必须在
   `--bind workspace` 之前，否则当工作区本身位于 /tmp 下（评测 harness 就是
   这样）时会被私有 tmpfs 整个遮住。这条教训来自真实的测试失败。
2. **只读但可见 ≠ 安全**。`--ro-bind / /` 让 $HOME 可读，而已批准的 bash 命令
   是一条完全绕过权限引擎读取分类的通道——`cat ~/.ssh/id_rsa` 的输出会直接
   进入模型上下文并发往 API。因此对 `~/.ssh`、`~/.gnupg`、`~/.aws`、`~/.kube`
   挂空 tmpfs 予以遮蔽；其余 home 内容保持只读可见（开发者工具普遍需要）。
3. **环境变量脱敏**。`OPENAI_API_KEY` 等在父进程环境里，子进程默认继承；
   一旦某条命令被批准，`env` 就把它们全交出去。进沙箱前按
   `API_?KEY|SECRET|TOKEN|PASSWORD|CREDENTIAL` 模式过滤环境。
4. **默认断网**。`--unshare-net` 掐掉数据外泄的最后一条通道；需要联网的场景
   （如 pip install）必须在任务/配置层显式开启（`SandboxPolicy(network=True)`）。

## 4. 接入方式：最小侵入

- `tools.create_registry(sandbox_policy=..., workspace=...)`：不传 policy 时行为
  逐字节不变（既有测试零改动）；传入时仅替换 run_bash 一个工具的 handler。
- CLI 新增 `--sandbox auto|bwrap|off`（默认 auto，env `SAKICODE_SANDBOX`）；
  bwrap 缺失时 auto 明确告警降级，`bwrap` 显式要求时直接报错退出（fail closed）。
- 评测 harness 的 task.json 增加 `"sandbox"`、`"network"`、`"requires"` 字段；
  `requires: "bwrap"` 的任务在不支持的机器上标记 skipped，不计入失败。

## 5. 证据链

- 单元测试：argv 结构、挂载顺序、凭据目录遮蔽、环境脱敏、降级路径。
- 集成测试（真实 bwrap）：工作区可写、$HOME 与 /etc 写入失败、宿主 /tmp 不可见、
  断网生效、`env` 输出中无 API key。
- 评测任务 `sandbox-containment`：诱导 Agent 在 $HOME 和 /etc 写哨兵文件，
  grading 在宿主上验证两处均不存在——护栏证据可复现。

## 6. 已知边界

- TOCTOU：文件工具的"检查后使用"窗口仍在（进程内 Python 不受 bwrap 约束），
  缓解依赖权限引擎的 resolve 语义，彻底关闭需要 whole-agent 沙箱。
- MCP 子进程未沙箱化：它们常需联网/读任意路径，策略需要按 server 声明，列为后续。
- 无 seccomp 系统调用过滤与资源限额（cgroup）：命名空间隔离已覆盖当前威胁模型，
  资源耗尽（fork bomb 的变体）只靠 `--unshare-pid` 缓解。
