# Next Steps

更新时间：2026-08-24

## 当前共识

旧 `note.py` 中记录的以下规划已经完成，不再作为待办：

1. Starter 验收 Skill；
2. SequentialFlow 与显式 handoff；
3. RunHandle / `start()`；
4. HTTP SSE；
5. 取消与 ContextPolicy；
6. Planner / Executor。

当前最新实现已经完成 `EXECUTOR_DESIGN.md` 中 Phase A、Phase B 和 Phase C：Agent 与
Flow 共用 ExecutionRequest、ExecutionAttempt、lease、fencing、recovery decision 和统一的
ExecutionWorker；verified Artifact receipt、side-effect evidence、稳定 Tool operation identity 和
细粒度恢复门禁已进入内存/PostgreSQL 执行面。

## 本次完成：Phase C 恢复门禁

Phase C 最后一项已经完成：

1. 新增 ArtifactReceipt、SideEffectEvidence 和 RecoveryGateBlocker 模型；
2. 内存/PostgreSQL ExecutionStore 持久化证据，PostgreSQL 与 Event 共用 fencing 事务；
3. FlowCheckpoint 固定 Artifact ID，并只验证 Host 接受/选择且 metadata 一致的 receipt；
4. Tool operation ID/idempotency key 跨 Attempt 稳定，intent hash 阻止 key 被不同调用复用；
5. read-only、idempotent、mutating、unknown 按 checkpoint 覆盖范围执行逐策略门禁；
6. 恢复查询投影 allowed strategies、receipt/evidence 和 blockers，Store 在入队前再次强制校验；
7. 补充内存、HTTP、Flow、Worker/PostgreSQL schema 与可选集成测试，并更新权威文档。

## 验收结果

- Flow 只能从已验证、已持久化的 Stage/Artifact 边界恢复；
- 旧 Attempt 不能提交 Event、Artifact receipt、side-effect evidence 或终态；
- read-only 与 intent 一致的 idempotent operation 可以重放；
- checkpoint 外的 mutating/unknown operation 阻止对应策略，无法通过 Operator reason 绕过；
- checkpointed WAITING operation 可由新 Attempt 结算，同时保留原 owner 与 resolution fencing；
- SDK、Starter 和 MTBI 离线测试通过；PostgreSQL 集成用例在配置测试 DSN 时执行。

## 下一步：Phase D

Phase D 优先顺序为：

1. bounded concurrency、backpressure 和配额；
2. drain deadline、Tool/Run/progress timeout；
3. metrics、tracing 和告警；
4. 可选外部 broker Adapter；
5. checkpoint migration 与旧 Runtime 保留策略，包括 `NEXT-RUN-001`。

权威设计依据：`EXECUTOR_DESIGN.md` 的 Phase C/Phase D，以及 `ARCHITECTURE.md` 的后续迁移顺序。
新会话开始时先阅读本文件，再检查 Git 状态和上述两份设计文档是否已有更新。
