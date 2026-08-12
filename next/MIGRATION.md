# Migration Boundary

旧 `src/base_agent` 是行为和生产边界的参考，新 `next/src/base_agent` 是公共 API 的重新
设计。迁移遵循“按契约重写，按需复用测试”，不按目录复制。

## 允许迁入

- Provider 响应归一化的协议经验；
- Tool 参数推导、权限、超时、副作用和确认规则；
- Run/Event 关联标识与有序事件语义；
- 预算、取消、无进展检测等纯运行规则；
- Attachment、Artifact、Resource、Store 的小型 Protocol；
- FakeModel 和确定性 Harness 测试方法；
- AgentDemo 的阶段 Prompt、输入交接、产物验证；
- mock-manus 的实时事件和 Planner/Executor 闭环；
- Hermes 的固定上下文快照、readiness、渐进披露和窄腰原则。

## 不直接迁入

- 旧根包的全量导出；
- AgentProfile 兼容层；
- AgentRuntimeInvoker、FlowLifecycle、FlowWorker 等内部类型；
- 要求普通 Agent 开发者理解 Repository 聚合、事务或恢复状态机；EventStore 作为可选
  extension port 保留默认内存实现；
- 深继承 Agent/Flow；
- import-time 自动注册和全局可变 Tool registry；
- Gateway、Profile routing、插件市场和产品 UI；
- 没有真实应用需求的并行、分布式或自治多 Agent 抽象。

## 迁移门槛

一项旧能力只有同时满足以下条件才进入新内核：

1. AgentDemo、mock-manus、Hermes 或实际业务场景能够证明需求；
2. 普通开发者无需理解其内部生命周期；
3. 可以通过内存实现和 FakeModel 做确定性测试；
4. 不扩大根包主路径；
5. 默认不扩大 Agent 权限。
