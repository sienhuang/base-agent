# Code Review Bugs

记录日期：2026-07-29

验证基线：

- `pytest`：166 passed，8 skipped
- Ruff：通过
- Mypy strict：通过

## BUG-001：保存运行快照可能覆盖并丢失取消请求

严重度：P1
状态：已修复（2026-07-31）

涉及位置：

- `src/base_agent/runtime/persistence.py:12`
- `src/base_agent/runtime/persistence.py:52`
- `src/base_agent/stores/in_memory.py:322`
- `src/base_agent/stores/postgres/store.py:299`

### 问题

`save_context_snapshot()` 先读取完整的 `Run`，再基于这个对象生成新快照并整体写回。
如果 `request_cancel()` 恰好发生在 `get()` 和 `save()` 之间，快照中旧的
`cancel_requested=False` 会覆盖已经持久化的 `True`。

受影响的 `InMemoryRunStore` 和 `PostgresStore` 都没有版本检查或字段级原子合并。

### 影响

调用方已经成功请求取消，但 Runtime 看不到取消标志，运行可能继续执行并最终以
`COMPLETED` 结束。

最小复现结果：

```text
requested True
result completed
stored completed
cancel_requested False
```

### 修复建议

- 将 Run 更新设计成原子 patch 或 compare-and-swap。
- 为 Run 增加版本字段并在保存时检查版本。
- 保证 `cancel_requested` 只能从 `False` 单调更新为 `True`，普通运行快照不得将其回退。
- 为内存和 PostgreSQL 两个实现增加确定性的并发回归测试。

### 修复结果

- `InMemoryRunStore.save()` 在同一把锁内读取当前 Run 并单调合并
  `cancel_requested`，旧快照不能清除已经接受的取消请求。
- `PostgresStore._save_run()` 使用 `SELECT ... FOR UPDATE` 与
  `request_cancel()` 串行化，并将合并后的 Run 同时写入 JSON payload 和索引列。
- 新增确定性的 Runtime 并发测试，固定复现“快照读取后、保存前发生取消”的时序；
  Run 最终为 `CANCELLED` 且取消标志保持为 `True`。
- PostgreSQL 回归测试覆盖取消后写入旧快照，确认取消标志不会回退。

## BUG-002：恢复任务被取消后 Run 停在 RUNNING 且检查点丢失

严重度：P1
状态：已修复（2026-08-03）

涉及位置：

- `src/base_agent/agent.py:286`
- `src/base_agent/agent.py:332`
- `src/base_agent/runtime/engine.py:272`

### 问题

`Agent.resume()` 首先通过 `CheckpointStore.claim()` 原子取走检查点。Runtime 随后将
Run 更新为 `RUNNING`。

如果恢复任务在执行过程中收到 `CancelledError`：

1. `AgentRuntime.execute()` 重新抛出异常；
2. 结果构建、检查点保存和 Run 最终化均被跳过；
3. `Agent.resume()` 的 `BaseException` 分支只记录日志，没有恢复已经 claim 的检查点。

### 影响

Run 永久停在 `RUNNING`，原检查点已经不存在。后续再次调用 `resume()` 会因为 Run
不是 `WAITING` 而被拒绝，运行无法继续。

最小复现结果：

```text
run_status running
checkpoint missing
```

普通 `Agent.run()` 被任务级取消时也会跳过 durable Run 最终化，可能遗留
`RUNNING` 状态。

### 修复建议

- 区分显式业务取消与底层 asyncio 任务中断，避免混用 `CANCELLED`。
- 当前不承诺恢复运行中任务；任务中断必须持久化为明确的不可恢复终态。
- 中断收尾必须删除 Checkpoint、释放 Conversation Turn，并发送唯一终态事件。
- 增加首次运行、恢复、Conversation 和事件流的确定性回归测试。

### 修复结果

- 新增 `INTERRUPTED` Run、Execution 和 Result 状态，以及永久终态事件
  `RUN_INTERRUPTED`。
- Runtime 捕获 `CancelledError` 后先释放 Resource，再持久化不可恢复的
  `INTERRUPTED`、删除 Checkpoint、释放 Conversation Turn 并发送一次
  `RUN_INTERRUPTED`，最后继续向调用方传播 `CancelledError`。
- 如果 Run 已经接受了显式 `Agent.cancel()` 请求，业务取消优先，仍以
  `CANCELLED` / `RUN_CANCELLED` 收尾。
- Agent facade 覆盖创建 Run 和 claim Checkpoint 附近的边缘中断；幂等兜底不会重复发送
  终态事件。
- 新增首次运行中断、恢复中断、Conversation 释放、Checkpoint 删除、状态机转换和事件流
  永久边界测试。当前版本不支持恢复 `INTERRUPTED` Run。

## BUG-003：Browser 私网策略存在 DNS rebinding 绕过

严重度：P1

涉及位置：

- `src/base_agent/browser/playwright.py:54`
- `src/base_agent/browser/playwright.py:121`

### 问题

`BrowserNetworkPolicy.check()` 先使用系统 DNS 验证主机地址，Playwright 随后在
`route.continue_()` 时自行重新解析并建立连接。策略验证的地址和实际连接地址没有绑定。

攻击者控制的域名可以第一次解析到公网地址以通过检查，随后解析到 loopback、RFC1918
私网地址或云元数据地址，从而绕过 `allow_private_network=False`。

### 影响

启用 Browser Tool 的 Agent 可能被诱导访问内部服务或云元数据端点，破坏项目声明的
Browser 网络安全边界。

### 修复建议

- 通过受控网络代理统一完成 DNS 解析、策略验证和实际连接；或
- 将已验证的解析结果固定到实际连接，避免浏览器再次独立解析。
- 不要依赖重复调用 `getaddrinfo()` 解决检查与使用之间的 TOCTOU。
- 增加 DNS rebinding 和跳转到私网地址的安全测试。

## BUG-004：同步 Tool 超时后仍会在后台继续执行

严重度：P2

涉及位置：

- `src/base_agent/tools/decorator.py:78`
- `src/base_agent/tools/executor.py:64`

### 问题

同步 Tool 通过 `asyncio.to_thread()` 执行。外层 `asyncio.timeout()` 只能停止等待线程
结果，无法终止已经运行的 Python 线程。

因此 Runtime 返回 `ToolResultStatus.TIMEOUT` 后，同步函数仍可能继续产生副作用。

最小复现结果：

```text
reported timeout
finished_at_return False
finished_later True
```

### 影响

模型或上层调度器可能认为 Tool 没有执行并进行重试，导致重复写文件、重复数据库更新，
或者与仍在后台运行的第一次调用并发修改外部状态。

### 修复建议

- 禁止同步副作用 Tool，要求其使用支持协作取消的异步实现；或
- 将同步 Tool 放入可终止的独立进程或沙箱中执行；或
- 明确定义 timeout 仅代表“停止等待”，并阻止超时操作被自动重试。
- 增加验证超时后副作用不会继续发生的测试。
