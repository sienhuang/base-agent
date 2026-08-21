# 独立 Executor、ExecutionAttempt 与人工恢复设计

实现状态（2026-08-20）：Phase A 与顶层普通 Agent Run 的 Phase B 纵向链路已落地；FlowRunStore、
Flow Worker、共享 Artifact receipt、side-effect evidence 与 bounded concurrency 仍属于后续阶段。
默认执行模式继续是 `LocalRunExecutor`。

## 1. 目标与明确决策

这一阶段解决四个已经由 MTBI 和 Kubernetes 部署场景证明的问题：

1. FastAPI 发布或故障不能中断独立 Worker 中正在运行的任务；
2. Tool 可以运行数分钟甚至更久，但执行 lease heartbeat 不能与 Tool 共用同一个 Event Loop；
3. 一个逻辑 Run 或 Flow 可以跨多个 Executor、多个 Pod 和多个 ExecutionAttempt 继续；
4. Executor 失联后，框架只提供事实、状态和恢复接口，不自动判断未知副作用能否重放。

因此固定以下决策：

- API Server 和 Executor Worker 是独立进程、可独立部署；
- Executor Supervisor 持有 lease，Agent/Flow/Tool 在受监管子进程中执行；
- Run/Flow 是逻辑任务，ExecutionAttempt 是一次临时执行所有权；
- lease 只证明 Supervisor 存活，不代表 Tool 有业务进展；
- lease 时长按故障发现窗口配置，与任务总耗时无关；
- PostgreSQL 使用数据库时间计算 claim、heartbeat 和过期，不使用 Pod 本地时钟；
- lease 过期只进入 `RECOVERY_REQUIRED`，不自动 retry；
- 继续、取消或确认失败由受信任 Operator 接口显式提交；
- 同一 Run/Flow 恢复时保留原 ID，创建新的 `attempt_id`；
- 普通 Agent Run 先落地，Flow 在持久 Stage/Plan checkpoint 就绪后接入同一执行面；
- `LocalRunExecutor` 保留为零基础设施默认实现，但不作为长任务跨 Pod 恢复方案。

本文不承诺 arbitrary Python stack continuation、exactly-once Tool 执行或 Kubernetes
Pod API 作为基础正确性依赖。

## 2. 进程与职责边界

```text
Client
  │
  ▼
FastAPI Control Plane
  ├── 创建/查询 Run 与 Flow
  ├── Event/SSE
  ├── WAITING resume/confirm
  ├── cancel request
  └── 受信任 Operator recovery decision
          │
          ▼
PostgreSQL
  ├── logical Run/Flow state
  ├── execution requests
  ├── execution attempts + lease/fencing token
  ├── checkpoints
  ├── recovery decisions
  └── events/artifact metadata
          ▲
          │ claim / heartbeat / fenced settlement
Executor Supervisor
  ├── 停止/开始领取任务
  ├── 定时续租与周期性 reconcile
  ├── 监管 timeout/cancel/shutdown
  └── 启动一个 Execution Child
          │ IPC
          ▼
Execution Child Process
  ├── 构造固定版本 Agent/Flow
  ├── Model/Tool loop
  ├── 同步或异步 Tool
  └── 产生 Event、Artifact receipt 和最终结果
```

### 2.1 FastAPI Control Plane

FastAPI 不保存活动 `asyncio.Task`，也不执行生产长任务。它负责：

- 在事务边界创建逻辑 Run/Flow 和待执行 request；
- 查询持久状态与 Event；
- 把用户 cancel、WAITING resume/confirm 转换成持久 command；
- 在现有 Run/Flow 查询中投影恢复事实，并暴露经过认证授权的人工恢复决策接口；
- 在分布式模式下即使所有 Worker 停止，也保持控制面可读。

FastAPI 滚动发布不会停止 Execution Child，也不会影响 Supervisor heartbeat。

### 2.2 Executor Supervisor

Supervisor 是 Worker Pod 的主进程，负责且只负责执行控制：

- 用 `FOR UPDATE SKIP LOCKED` 领取一个可执行 request；
- 原子创建新的 ExecutionAttempt、分配 fencing token、推进逻辑状态；
- 在独立于 Child 的调度循环中更新 heartbeat；
- 监听 Child 的 IPC 消息并执行带 fencing 的状态/Event 提交；
- 处理用户取消、Tool/Run deadline、Child 退出和 Worker shutdown；
- 周期性扫描已过期 Attempt，将其标记为失联；
- 不根据 Prompt、异常正文或 LLM 输出做恢复安全判断。

### 2.3 Execution Child Process

Child 不拥有 lease，也不能直接改变逻辑 Run/Flow 的权威状态。它通过受限 IPC 向 Supervisor
报告：

```text
child.started
child.event
child.checkpoint
child.waiting
child.succeeded
child.failed
child.cancelled
```

Supervisor 在持有当前 fencing token 时才接受并持久化消息。旧 Child 即使在 lease 丢失后
迟到返回，也不能写入新的 Attempt 或覆盖逻辑终态。

同步 pandas、阻塞 SDK 或 CPU 计算只会占用 Child；Supervisor heartbeat 仍可运行。Child
永久卡死时 lease 仍会续租，因此必须另外使用 Tool timeout、Run deadline 或 progress
deadline，不能用 lease 代替执行超时。

第一版每个 Worker 可以只并发一个 Child，后续再增加有界并发；并发数属于部署容量配置，
不进入 Agent Prompt 或 Definition。

## 3. 三层状态，不再混用

### 3.1 逻辑 Run/Flow 状态

Agent Run 与 Flow Run 对齐以下控制面状态：

| 状态 | 含义 | 是否可被 Worker 直接领取 |
| --- | --- | --- |
| `CREATED` | 逻辑记录已创建，尚未成功提交执行 request | 否 |
| `QUEUED` | 已存在可执行 request，等待 Worker | 是 |
| `RUNNING` | 当前存在一个有效 ExecutionAttempt | 否 |
| `WAITING` | 已持久化 checkpoint，等待用户输入或确认 | 否 |
| `RECOVERY_REQUIRED` | Attempt 中断或 lease 过期，等待人工决定 | 否 |
| `BLOCKED` | 已知定义、Artifact、权限或运行环境前置条件不满足 | 否 |
| `SUCCEEDED` | 逻辑任务成功终态 | 否 |
| `FAILED` | 明确失败终态 | 否 |
| `CANCELLED` | 用户或 Operator 明确取消终态 | 否 |

`INTERRUPTED` 不再作为逻辑 Run 的终态；它属于 Attempt。迁移期可以继续读取历史
`run.interrupted`，但新执行面将其投影为 `RECOVERY_REQUIRED` 或明确终态。

`SUSPECTED` 不是持久业务状态。监控可以根据 heartbeat 延迟显示 `lease_health=suspected`，
但在 lease 真正过期前不能创建第二个 Attempt。

逻辑状态机：

```text
CREATED ── submit ──> QUEUED ── claim ──> RUNNING
                         ▲                    │
                         │                    ├── result ──────> SUCCEEDED
                         │                    ├── failure ─────> FAILED
                         │                    ├── user cancel ─> CANCELLED
                         │                    ├── checkpoint ──> WAITING
                         │                    ├── prerequisite ─> BLOCKED
                         │                    └── owner lost ──> RECOVERY_REQUIRED

WAITING ── resume/confirm request ────────────────────> QUEUED
RECOVERY_REQUIRED/BLOCKED ── Operator retry decision ─> QUEUED
RECOVERY_REQUIRED/BLOCKED ── Operator cancel decision ─────────> CANCELLED
RECOVERY_REQUIRED/BLOCKED ── Operator fail decision ───────────> FAILED
```

`WAITING` 与 `RECOVERY_REQUIRED` 不同：前者明确知道需要什么用户输入并拥有可恢复 checkpoint；
后者只知道上一次执行所有权已结束，必须先由 Operator 选择恢复策略。

### 3.2 ExecutionAttempt 状态

一次逻辑 Run/Flow 可以有多个 Attempt：

| 状态 | 含义 |
| --- | --- |
| `CLAIMED` | Worker 已领取，尚未确认 Child 启动 |
| `STARTING` | 正在构造固定 Runtime/启动 Child |
| `RUNNING` | Child 已启动且 Supervisor 正常 heartbeat |
| `STOPPING` | 正在执行 cancel、deadline 或 graceful interrupt |
| `SUSPENDED` | Child 产生 WAITING checkpoint，本 Attempt 正常结束 |
| `SUCCEEDED` | 本 Attempt 产生逻辑成功结果 |
| `FAILED` | 本 Attempt 明确失败 |
| `CANCELLED` | 因用户明确取消而结束 |
| `INTERRUPTED` | Supervisor 在 graceful shutdown 中主动中断 |
| `LEASE_EXPIRED` | Supervisor 未能续租，由 reconciler 确认失联 |

示例：

```text
Run-123: RUNNING → RECOVERY_REQUIRED → QUEUED → RUNNING → SUCCEEDED

Attempt-1: RUNNING → LEASE_EXPIRED
Attempt-2: CLAIMED → RUNNING → SUCCEEDED
```

### 3.3 ExecutionRequest 状态

ExecutionRequest 表示一次明确的执行意图，而不是 Run 本身：

| 状态 | 含义 |
| --- | --- |
| `PENDING` | 等待 Worker 领取 |
| `CLAIMED` | 已绑定当前 Attempt |
| `COMPLETED` | 本 request 已正常结算 |
| `INTERRUPTED` | Attempt 中断或 lease 过期 |
| `BLOCKED` | Worker 解析到确定的前置条件问题 |
| `CANCELLED` | request 在执行前或执行中被取消 |
| `DISCARDED` | Operator 明确不再执行该 request |

同一次人工 retry 创建新的 ExecutionRequest 和新的 Attempt，不复用旧 token。request
使用 idempotency key 去重；相同 key、不同 intent 必须冲突失败。

## 4. PostgreSQL 权威数据模型

第一版 PostgreSQL Adapter 使用单层 Attempt lease。外部消息队列未来可以有自己的 delivery
ack，但不能把 broker delivery token 暴露为逻辑执行 fencing token。

### 4.1 逻辑记录

现有 `base_agent_runs` 逐步迁移为纯逻辑状态，新增：

```text
current_attempt_id
current_request_id
recovery_reason_code
recovery_detected_at
definition_id/version/fingerprint
runtime_route
revision
```

当前直接存放在 RunRecord 上的 `lease_owner/token/expires_at/heartbeat_at` 迁移到 Attempt 表。
Flow 增加等价的 `base_agent_flow_runs`，并持久化 Plan/Stage checkpoint 与活动 child 关联。

### 4.2 ExecutionRequest

```text
base_agent_execution_requests
├── request_id
├── target_kind              # agent_run | flow_run
├── target_id
├── operation                # execute | resume | confirm | recover | cancel
├── status
├── payload                  # 有界 JSON；敏感输入遵循 Run 数据策略
├── runtime_route
├── idempotency_key
├── available_at
├── claimed_attempt_id
├── created_at
└── updated_at
```

唯一约束至少包括 `idempotency_key`；可领取索引覆盖
`(status, available_at, runtime_route)`。

### 4.3 ExecutionAttempt

```text
base_agent_execution_attempts
├── attempt_id
├── target_kind
├── target_id
├── request_id
├── attempt_number
├── status
├── owner_id                 # cluster/ns/pod_uid/process_uuid
├── fencing_token
├── runtime_build_id
├── child_pid
├── claimed_at
├── started_at
├── heartbeat_at
├── lease_expires_at
├── ended_at
├── reason_code
└── revision
```

`(target_kind, target_id, attempt_number)` 唯一。数据库约束或事务必须保证同一个逻辑目标只有
一个活动 Attempt。

### 4.4 RecoveryDecision

```text
base_agent_recovery_decisions
├── decision_id
├── target_kind
├── target_id
├── source_attempt_id
├── decision                 # retry | cancel | fail
├── strategy                 # from_checkpoint | from_stage | restart
├── subject_id
├── reason_code
├── expected_target_revision
├── idempotency_key
├── created_request_id
└── created_at
```

Decision 与逻辑状态转换、可选新 ExecutionRequest 在同一个事务提交。重复 idempotency key
返回同一结果；两个并发且意图冲突的决定只有一个成功。

### 4.5 数据库时间与 fencing

PostgreSQL 的 claim、renew、settle、expire 全部使用数据库时间，例如
`clock_timestamp()`，避免多节点本地时间偏差。核心更新都要求：

```sql
WHERE attempt_id = :attempt_id
  AND fencing_token = :token
  AND status IN ('claimed', 'starting', 'running', 'stopping')
  AND lease_expires_at > clock_timestamp()
```

领取使用 `FOR UPDATE SKIP LOCKED`。reconciler 也锁定行后再把 Attempt 转为
`LEASE_EXPIRED`，并用逻辑记录 revision/CAS 把 Run/Flow 转为 `RECOVERY_REQUIRED`。

权威状态转换及其生命周期 Event 必须在一个 Repository 事务中提交。普通 model/tool Event
由 Child 发送给 Supervisor；Event 至少携带 `attempt_id`，旧 Attempt 的迟到终态 Event 必须被
拒绝。Artifact 二进制可以直接写入受控 Store，但只有 Supervisor 接受的 receipt 能成为
逻辑 Run/Flow 的正式产物。

## 5. 领取、heartbeat 和长 Tool

Worker 领取一个 request 的事务：

1. 锁定一个 `PENDING AND available_at <= database_now` 的 request；
2. 校验目标仍为 `QUEUED` 且 revision 匹配；
3. 创建 Attempt，生成随机 fencing token；
4. request 转为 `CLAIMED`；
5. 逻辑目标转为 `RUNNING` 并记录 `current_attempt_id`；
6. 提交后启动 Child。

Supervisor 每隔 heartbeat interval 续租。典型配置可以是 heartbeat 5 秒、lease 30 秒、
reconcile 5 秒；任务执行五分钟或两小时都不改变 lease 配置。

```text
Supervisor: renew renew renew renew ... settle
Child:      └──────── Tool 执行 5 分钟 ────────┘
```

如果 Child 阻塞，heartbeat 不受影响。如果 Supervisor 无法续租：

1. 立即停止接受 Child 的权威结果；
2. 尝试终止 Child；
3. 由数据库 fencing 保证迟到结果不能提交；
4. lease 到期后 reconciler 进入人工恢复流程。

## 6. Reconciler 只识别，不决定

每个 Worker Supervisor 都可以运行周期性 reconciler；PostgreSQL 行锁和 `SKIP LOCKED`
保证多个 reconciler 可以安全并发。也可以部署独立 maintenance worker，但基础正确性不依赖
单一 leader。

reconciler 的唯一职责：

```text
Attempt RUNNING + lease_expires_at <= database_now
  → Attempt LEASE_EXPIRED
  → ExecutionRequest INTERRUPTED
  → Run/Flow RECOVERY_REQUIRED(reason=owner_lease_expired)
  → execution.attempt.lease_expired + run/flow.recovery_required Event
```

它不会：

- 自动创建 retry request；
- 根据 Tool side-effect 枚举自行决定安全；
- 修改 Artifact 内容；
- 用当前版本 Runtime 静默替换原 snapshot；
- 在 lease 到期前通过 Pod 名称猜测 owner 已死亡。

这同时解决“新 Worker 在旧 lease 到期前已经启动”的情况：startup reconcile 可以无结果，
常驻循环会在真正到期后的下一个周期完成状态转换。

## 7. Graceful shutdown 与异常退出

### 7.1 Graceful Worker shutdown

Supervisor 收到 SIGTERM 后：

1. 停止领取新 request；
2. readiness 变为 false；
3. 允许活动 Child 在配置的 drain deadline 内继续，heartbeat 不停止；
4. Child 完成则正常结算；
5. deadline 到达仍未完成时，向 Child 发送 interrupt/cancel 协议信号；
6. 将 Attempt 标记为 `INTERRUPTED`、request 标记为 `INTERRUPTED`；
7. 将逻辑 Run/Flow 标记为 `RECOVERY_REQUIRED(reason=worker_shutdown)` 并释放 lease；
8. 不把部署中断伪装成用户 `CANCELLED`。

是否继续由 Operator 决定。graceful 路径由旧 Supervisor 主动提交，因此不需要等待 lease
自然到期。

### 7.2 kill、OOM 或节点故障

异常退出没有交接机会：

```text
heartbeat 停止
→ 等待 lease 硬过期
→ reconciler 标记 LEASE_EXPIRED / RECOVERY_REQUIRED
→ Operator 决定
```

lease 未过期时最多只能显示运维层 `suspected`，不能领取第二个 Attempt。需要更短 RTO 时应
缩短 lease，并保证 heartbeat 与数据库延迟有足够余量；不通过越过有效 lease 实现快速接管。

### 7.3 FastAPI shutdown

FastAPI 不持有 Child 或 Attempt lease。API Pod 的 SIGTERM、滚动发布和 `kill -9` 均不改变
正在执行的 Attempt；只影响短暂的提交与查询可用性。

## 8. 人工恢复接口

恢复接口属于受信任 Operator port，不与普通最终用户 HTTP 权限混用。身份认证和租户授权由
应用提供，Core 只要求审计字段和 CAS。

### 8.1 恢复上下文复用现有查询

```http
GET /v1/runs/{run_id}
GET /v1/flows/{flow_run_id}
```

当状态为 `RECOVERY_REQUIRED` 或 `BLOCKED` 时，现有响应增加有限的 `recovery` 投影：

```json
{
  "status": "recovery_required",
  "reason_code": "owner_lease_expired",
  "revision": 12,
  "last_attempt": {
    "attempt_id": "...",
    "status": "lease_expired",
    "owner_id": "...",
    "runtime_build_id": "...",
    "heartbeat_at": "...",
    "lease_expires_at": "..."
  },
  "definition": {
    "id": "...",
    "version": "...",
    "fingerprint": "...",
    "runtime_route": "..."
  },
  "checkpoint": null,
  "completed_stages": [],
  "artifacts": [],
  "side_effect_evidence": []
}
```

这不是新的事实源，也不要求客户端自己重放全部 Event。服务从逻辑 Run/Flow、最新 Attempt、
Checkpoint、Artifact metadata 和副作用证据 Store 聚合当前快照；Event 接口继续提供完整审计
历史。heartbeat 不写成高频 Event，因此仅靠 Event 不能得到精确的当前 lease 快照。

普通调用者只能看到其已有权限范围内的有限恢复摘要。Attempt owner、运行版本和副作用证据等
运维字段需要 Operator 权限才返回。响应不包含框架自动生成的“应该 retry”结论。

### 8.2 提交恢复决定

```http
POST /v1/operator/runs/{run_id}/recovery-decisions
POST /v1/operator/flows/{flow_run_id}/recovery-decisions
```

```json
{
  "decision": "retry",
  "strategy": "from_checkpoint",
  "expected_revision": 12,
  "subject_id": "operator-001",
  "reason_code": "artifacts_verified",
  "idempotency_key": "ticket-123-retry-1"
}
```

允许决定：

- `retry`：创建新 ExecutionRequest，逻辑状态转为 `QUEUED`；
- `cancel`：逻辑状态转为 `CANCELLED`；
- `fail`：逻辑状态转为 `FAILED`。

允许策略由目标类型决定：

- Agent Run：`from_checkpoint` 或 `restart`；
- Sequential Flow：`from_stage`、`from_checkpoint` 或 `restart`；
- Planner-ReAct Flow：必须依赖持久 Plan revision、Step 状态和 child 关联后再支持。

框架必须验证请求的 checkpoint/Stage/Definition 是否存在并匹配，但不替 Operator 选择策略。
由 Flow 拥有的 child Run 不能单独恢复；必须通过父 Flow recovery 接口，避免破坏 Plan/Stage
一致性。

现有 `/resume` 和 `/confirm` 继续只服务 `WAITING`，不与 recovery decision 混用。

## 9. Runtime 版本路由

ExecutionRequest 不保存活的 Agent/Flow Python 对象，只保存固定身份：

```text
definition_id
definition_version
definition_fingerprint
snapshot_fingerprint
runtime_route
```

Worker composition root 注册它能执行的不可变版本。Worker 只领取匹配 `runtime_route` 的
request，并在启动 Child 前再次校验 fingerprint。

滚动发布时，新 Run 进入新 route，旧 WAITING/RECOVERY_REQUIRED Run 仍可路由到保留窗口内的
旧 Runtime。没有兼容 Runtime 时进入 `BLOCKED(reason=runtime_unavailable)`，不能用当前版本
静默恢复。跨版本恢复需要显式 checkpoint migrator 和新的 Operator decision。

## 10. Flow 恢复边界

Flow 不尝试恢复任意 Python 调用栈，只恢复持久编排事实：

- Flow input；
- Definition/version/fingerprint；
- Plan revision 或 Sequential Stage index；
- child Run/Attempt 关联；
- Stage result/handoff；
- 已验证 ArtifactRef；
- WAITING checkpoint；
- side-effect evidence。

MTBI 示例：

```text
query.sql + dataset.csv 已验证
analysis Attempt 中断
→ Operator 选择 from_stage=analysis
→ 同一 flow_run_id 创建新 Attempt
→ 不重跑 query Stage
```

如果 SQL 子进程中断且没有完整、验证通过的 CSV，则只能由 Operator 选择重新运行 query；框架
不能把部分文件当成成功 checkpoint。

Planner-ReAct 必须先把 Plan、Step 和当前 planner/executor child 位置迁移到 FlowRunStore，
否则只能整 Flow restart，不能声称支持安全的 Step 级恢复。

## 11. 取消、超时和副作用

- `/cancel` 只设置持久 `cancel_requested`；当前 Supervisor 观察后停止 Child；
- 用户取消产生逻辑 `CANCELLED`，Worker shutdown 不产生该状态；
- Tool timeout、Run deadline 和 progress deadline 与 lease 独立；
- READ_ONLY 或下游幂等能力只是提供给 Operator 的证据，不触发自动 retry；
- ToolCall 使用稳定 operation ID/idempotency key；
- `started` 后未确认结果的非幂等副作用必须保留不确定状态；
- recovery context 只暴露 metadata 和摘要，不复制 Prompt、Tool 参数、密钥或大型结果。

## 12. 本地模式与分布式模式

公开 `RunExecutor` 边界应至少支持：

```text
LocalRunExecutor
PostgresWorkerExecutor / ExecutorSupervisor
```

本地模式继续允许：

```text
POST /runs → 当前进程立即创建并执行
```

分布式模式变为：

```text
POST /runs → CREATED + ExecutionRequest → QUEUED
agent-worker → claim → RUNNING
```

两种模式共享逻辑状态、Event 和 HTTP response，不要求普通 Agent 开发者理解 queue、Attempt
或 lease。`agent-server` 与计划新增的 `agent-worker` 只在应用 composition root 选择 Adapter。

## 13. 分阶段实现

### Phase A：模型和数据库正确性

1. 增加 `QUEUED`、`RECOVERY_REQUIRED` 逻辑状态；
2. 增加 ExecutionRequest、ExecutionAttempt、RecoveryDecision 模型和 Store Protocol；
3. PostgreSQL 建表、索引、CAS、fencing，统一使用数据库时间；
4. 把 lease 从逻辑 RunRecord 迁移到 Attempt；
5. 增加周期性 reconciler，但只标记人工恢复状态；
6. 保持 `LocalRunExecutor` 行为兼容。

### Phase B：独立 Agent Run Worker

1. 增加 `agent-worker` 入口和 Runtime version registry；
2. FastAPI 分布式模式只入队；
3. Supervisor claim/heartbeat/cancel/settle；
4. 每 Attempt 一个 Child process 和 IPC；
5. 在现有 Run/Flow response 中增加 recovery 投影，并增加 Operator decision 接口；
6. Starter 提供 PostgreSQL 手测配置。

### Phase C：Flow 持久化与恢复

1. FlowRunStore/FlowCheckpoint；
2. Sequential Stage index/result/Artifact checkpoint；
3. 父 Flow 统一恢复 child；
4. Planner-ReAct Plan revision/Step/child 位置持久化；
5. `/v1/operator/flows/.../recovery-decisions`。

### Phase D：生产强化

1. side-effect evidence 与稳定 idempotency key；
2. bounded concurrency、backpressure 和配额；
3. drain deadline、Tool/Run/progress timeout；
4. metrics/tracing/告警；
5. 可选外部 broker Adapter；
6. checkpoint migration 与旧 Runtime 保留策略。

## 14. 验收场景

设计落地必须覆盖：

1. API Pod 重启时五分钟 Tool 不受影响；
2. Child 内同步阻塞五分钟时 Supervisor heartbeat 持续更新；
3. Worker 在旧 lease 到期前快速重启，周期性 reconciler 在到期后识别失联；
4. 两个 Worker 同时 claim 时只有一个创建 Attempt；
5. 旧 token 的 heartbeat、Event、Artifact receipt 和终态提交全部被拒绝；
6. graceful Worker shutdown 把未完成 Attempt 标记为 `INTERRUPTED`，Run 进入
   `RECOVERY_REQUIRED`，不伪装成用户取消；
7. `kill -9` 后 Run 在 lease 到期后进入 `RECOVERY_REQUIRED`，不会自动 retry；
8. Operator 重复提交同一 idempotency key 只创建一个新 request/Attempt；
9. 同一个 Run 经 Attempt-1 失联、Attempt-2 成功后只产生一个逻辑成功终态；
10. WAITING 仍通过 resume/confirm，不能调用 recovery decision 绕过 request ID；
11. Flow 只从已持久、已验证的 Stage/Artifact 边界恢复；
12. 没有兼容 Runtime 或 snapshot 时进入 `BLOCKED`，不静默使用新版本。

## 15. 非目标

- 把正在运行的 Python 协程、pandas 内存对象或本地子进程迁移到另一 Pod；
- 自动判断任意外部副作用是否安全；
- 仅凭 Kubernetes Pod 删除事件越过仍有效的 lease；
- 用无限 `terminationGracePeriodSeconds` 代替持久执行；
- 默认引入 Kafka、Redis、Celery 或特定工作流产品；
- 让 LLM 选择 Attempt、lease、恢复策略或 Operator 决策。

这个设计的核心不是“自动重跑”，而是把逻辑任务、执行尝试和人工决定分开：系统保证事实、
所有权、fencing 和可审计状态，Operator 决定是否以及从哪个安全边界继续。
