---
marp: true
theme: default
paginate: true
size: 16:9
title: 为什么 Agent 任务不能自动重试
description: base-agent 的执行所有权、故障恢复与副作用安全
---

<!-- 正文约 18 分钟，预留 2 分钟弹性或问答。 -->

# 为什么 Agent 任务不能自动重试？

## base-agent 的执行所有权、故障恢复与副作用安全

> 系统看见 Worker 死了，不代表系统知道 Tool 有没有成功

<!--
约 1 分钟。

今天只讨论一个问题：一个 Agent Tool 已经调用外部系统，但 Worker 在保存结果前宕机，此时任务能不能自动重试？

这个问题表面上是任务队列问题，本质上同时涉及执行所有权、分布式并发、外部副作用和可证明的恢复边界。base-agent 的执行面主要就是围绕这个问题建立的。
-->

---

# 最危险的故障窗口

```text
ToolCall: charge(order-42)
        │
        ├─ tool.started 已提交
        ├─ 支付系统可能已经扣款
        ├─ Worker / Pod 宕机
        └─ tool.completed 没有提交
```

系统只知道：

```text
started + owner lost = outcome unknown
```

| 选择 | 风险 |
|---|---|
| 自动重试 | 可能重复扣款 |
| 直接标记失败 | 可能实际已经扣款 |
| 假装成功 | 没有完成证据 |

<!--
约 2 分钟。

这是整场分享的冲突点。外部副作用和本地状态提交不是一个原子事务。Tool 可能已经成功改变外部世界，但本地只留下 started；也可能在真正执行前就死掉。这两种情况对数据库看起来一样。

所以 Worker 失联不是一个可以无条件 retry 的普通异常。base-agent 也不承诺 exactly-once Tool 执行，它选择保留不确定性，而不是伪造确定性。
-->

---

# 先拆开三个被混用的状态

```text
Logical Run / Flow       业务任务身份与最终状态
        │
        ├── ExecutionRequest   一次明确执行意图
        │
        └── ExecutionAttempt   一次临时执行所有权
                    │
                    └── RecoveryDecision  人工恢复决定
```

```text
Run-123: RUNNING → RECOVERY_REQUIRED → QUEUED → RUNNING → SUCCEEDED

Attempt-1: RUNNING → LEASE_EXPIRED
Attempt-2: CLAIMED → RUNNING → SUCCEEDED
```

**Run 可以延续，但 Attempt 的所有权不能复活。**

<!--
约 3 分钟。

第一个关键设计是分开逻辑任务和物理执行。

Run 或 Flow 是业务上稳定的任务身份；ExecutionRequest 表示 execute、resume、confirm 或 recover 这样的明确意图；ExecutionAttempt 是某个 Worker 临时持有的一次执行所有权。

同一个 Run 恢复时保留 run_id，但创建新的 request、attempt_id 和 fencing token。Attempt-1 过期后永远不能重新变成当前 owner。

RecoveryDecision 再单独记录由谁、基于什么理由、选择什么策略继续。这样系统检测事实，Operator 作出决定，两者不会混成一次自动状态跳转。
-->

---

# 控制面与执行面为什么要分进程？

```text
Client → FastAPI Control Plane
                   │
                   ▼
              PostgreSQL
       Run / Request / Attempt / Event
       Checkpoint / Evidence / Receipt
                   ▲
                   │ claim / heartbeat / fenced commit
          Executor Supervisor
                   │ IPC
                   ▼
            Execution Child
             Model / Tool loop
```

- API 发布不终止长任务
- Child 阻塞不阻塞 lease heartbeat
- Supervisor 持有 lease，Child 不拥有权威状态
- PostgreSQL 时间是多节点共同时间源

<!--
约 3 分钟。

第二个关键设计是职责隔离。

FastAPI 只做控制面：创建和查询任务、接收 cancel、resume、confirm 和 Operator decision。它不持有生产长任务的 asyncio.Task，因此 API Pod 滚动发布不会直接杀死执行任务。

Supervisor 负责 claim、heartbeat、timeout、cancel 和 settlement；真正的模型与 Tool 在 Child 进程里运行。即使 pandas、同步 SDK 或 CPU 工作阻塞 Child，Supervisor 的 heartbeat 仍能继续。

lease 只表示 Supervisor 还活着，不表示 Tool 有业务进展。因此 lease timeout 与 Tool timeout、Run deadline 是三套不同语义。
-->

---

# Lease 发现失联，Fencing 阻止“僵尸写入”

```text
Attempt-1 / token=41         Attempt-2 / token=42
       │ lease expires              │ new owner
       │                            │
       └──── late tool.completed ───X  rejected
```

关键提交条件：

```sql
WHERE attempt_id = :attempt_id
  AND fencing_token = :token
  AND status IN ('claimed', 'starting', 'running', 'stopping')
  AND lease_expires_at > clock_timestamp()
```

Reconciler 只执行：

```text
LEASE_EXPIRED → RECOVERY_REQUIRED
```

它不会自动创建 retry。

<!--
约 3 分钟。

lease 解决的是怎样发现 owner 失联；fencing 解决的是失联 owner 又迟到返回怎么办。

所有 heartbeat、Event、Artifact receipt 和最终 settlement 都必须携带当前 Attempt 的 fencing token，并且 lease 仍然有效。旧 Child 即使没有真正死亡，迟到的 tool.completed 或 run.succeeded 也无法覆盖新状态。

claim、renew 和 expire 使用数据库时间，避免 Pod 时钟漂移。lease 过期后 Reconciler 只把事实标记成 RECOVERY_REQUIRED，不根据异常文本或 Prompt 猜测能否重放。
-->

---

# 能否恢复，由持久证据决定

恢复查询聚合两类关键证据：

| 证据 | 作用 |
|---|---|
| SideEffectEvidence | Tool 是否 started、waiting、completed 或 unknown |
| ArtifactReceipt | 产物是否由 Host 校验并进入 checkpoint |

```text
read_only                         → 可重放
idempotent + stable operation ID → 可重放
changed intent SHA-256           → 拒绝
mutating / unknown               → 拒绝
checkpoint 覆盖的已完成操作      → 可从边界后继续
未验证 Artifact receipt          → 拒绝
```

**Operator 选择策略，Store 执行确定性门禁。**

<!--
约 3 分钟。

第三个关键设计是 evidence-gated recovery。

每个 tool.started、tool.waiting、tool.completed 都在接受 Event 的同一个 fencing 事务里更新 SideEffectEvidence。Attempt 中断时，未结算的 started 或 waiting 会转为 unknown。

read-only 操作可以重放。idempotent 操作必须保留跨 Attempt 稳定的 operation ID，并用 intent SHA-256 保证同一个 key 没有被换成另一组 Tool 参数。mutating 或 unknown 操作如果没有被 checkpoint 覆盖，就生成 unsafe_side_effect_replay blocker。

Artifact 也不能因为存在文件就算可恢复。只有 Host 再次校验 ID、哈希、大小、producer、attempt 和 ToolCall，并写入 verified receipt 后，才能成为 checkpoint 的恢复依据。

Operator 决定 retry、cancel 或 fail，但无法绕过 Store 根据持久证据计算的门禁。
-->

---

# 故障注入 Demo

```bash
.venv/bin/python examples/recovery_safety_demo.py
```

```text
Scenario A: mutating
lease expired → RECOVERY_REQUIRED
side effect   → UNKNOWN
late commit   → fenced
restart       → unsafe_side_effect_replay

Scenario B: idempotent
stable operation ID + same intent → retry allowed
same operation ID + changed intent → rejected
```

对应契约测试：

```bash
.venv/bin/pytest -q tests/test_execution.py \
  -k 'uncheckpointed_mutating or idempotent_evidence'
```

<!--
约 2 分钟。

Demo 不调用真实模型和外部服务，而是直接对执行协议做故障注入。这样展示的是系统正确性，而不是一次碰巧成功的模型输出。

第一组模拟 mutating Tool 已经 started，随后 lease 过期。系统把 SideEffectEvidence 转成 unknown，拒绝旧 Child 的迟到 completion，并阻止 restart。

第二组把操作声明为 idempotent。稳定 operation ID 和相同 intent 可以创建新 Attempt；如果复用 operation ID 但改变 intent hash，仍然立即拒绝。
-->

---

# base-agent 选择“不伪造确定性”

明确不承诺：

- arbitrary Python stack continuation
- exactly-once 外部 Tool 执行
- 仅凭 Pod 删除事件越过有效 lease
- 自动判断任意副作用是否安全
- 用新 Runtime 静默恢复旧 snapshot

最终原则：

> 系统保证事实、所有权、fencing 和恢复门禁；人决定是否以及从哪个安全边界继续。

<!--
约 1 分钟。

这套设计的代价是更多状态、事务和 Operator 流程，但它没有用“自动重试”掩盖无法证明的外部事实。

base-agent 真正的重点不是包了一层模型 API，而是把 Agent 长任务中最危险的不确定窗口，转换成可观察、不可越权、可审计决策的状态。
-->
