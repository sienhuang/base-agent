# next architecture

## 1. 唯一主路径

```text
业务代码
  Prompt / @tool / Skill
          │
          ▼
  AgentDefinition + Agent
          │
          ▼
       Harness
  snapshot / model-tool loop
  readiness / governance
  events / verification
          │
          ▼
       RunResult
```

开发者创建单 Agent 时不接触 Harness 实例。`Agent` 是 composition facade，`Harness` 是
包内运行实现。

简单多 Agent 只增加一层：

```text
Stage(agent, prompt, input_from, handoff, verify)
          │
          ▼
Flow.sequence(...)
          │
          ▼
      FlowResult
```

## 2. 模块职责

| 模块 | 职责 | 是否属于根 API |
| --- | --- | --- |
| `definitions.py` | 不可变 AgentDefinition | 是 |
| `tools/` | Tool、Toolkit、schema 和 readiness | 常用类型是 |
| `skills/` | Skill Descriptor/Catalog、按需加载、Package 与能力贡献 | 是 |
| `agent.py` | 普通应用的 Agent 门面 | 是 |
| `run_handle.py` | 进程内后台 Run 等待、状态、事件回放和实时订阅 | 是 |
| `events/` | EventStore/EventStream 契约和默认内存事实源 | 扩展 API |
| `context/` | 每次 Model Request 的只读上下文投影、预算和选择契约 | 扩展 API |
| `memory/` | 显式作用域的长期 Memory recall 与非阻塞写入队列契约 | 扩展 API |
| `server/` | 可选 FastAPI Agent/Flow 提交、状态、Event/SSE 和进程内 registry | 否 |
| `flows/` | SequentialFlow，以及固定角色、Host-owned Plan 的 PlannerReActFlow | 是 |
| `runtime/` | snapshot、model/tool loop、结果边界 | 否 |
| `governance/` | 副作用分类和 fail-closed 确认 | 常用枚举是 |
| `observability/` | 只读 Observer 契约 | 按需 |
| `verification/` | Evidence 值和验证契约 | 常用 Evidence 是 |
| `models.py` | Provider-neutral request/response/event 值 | 按需 |
| `providers/` | ModelProvider Protocol | 按需 |
| `testing/` | FakeModel 和测试策略 | 按需 |

依赖只能从应用门面流向 Harness 和小型值对象；`harness.py` 不得反向依赖 Flow、HTTP、
Worker 或具体 Provider。

`starter/` 是独立的下游验收应用，不属于 SDK 包。它在任一时刻只包含当前正在验收的
能力步骤，而不是所有框架能力的展示集合：

```text
starter/agent_app
    │ 只依赖 public / extension API
    ▼
base_agent facade ──> runtime Harness
```

如果 starter 为完成普通业务场景而必须导入 `base_agent.runtime`，视为 SDK API 设计失败。

## 3. 三套参考实现如何落地

| 来源 | 已落地原则 |
| --- | --- |
| AgentDemo | 薄 Agent、Tool 能力组合、Stage Prompt、显式交接和阶段验证 |
| mock-manus | typed Plan/Step、逐步 Review/Replan、已执行前缀不可修改和 Flow 实时事件 |
| Hermes | 窄腰核心、运行期固定 snapshot、Tool readiness、Observer fail-open、Governance fail-closed |

没有按源码复制任何一个项目的 Agent 基类、全局注册表、FlowFactory、Gateway 或产品配置。

## 4. 当前执行不变量

1. 一个 Run 开始后，system Prompt、Skill、Tool schema、权限和模型选择固定；
2. 未被 AgentDefinition 或 Skill 启用的 Tool 对模型不可见，也不可执行；
3. readiness 在第一次模型请求之前完成；
4. Tool 参数先验证，再检查确认，然后才执行；
5. Observer 只能读取 Event，异常不能改变执行；
6. Governance 默认拒绝无法取得的副作用确认；
7. Tool observation 有单结果和累计预算；
8. 结构化输出验证产生 Evidence；
9. Stage 失败或验证失败时，SequentialFlow 不继续下一个 Stage。

### 4.1 Flow Stage 拆分边界标准

默认使用单 Agent。Stage 表示由应用控制的独立 Run 边界，不表示单个 ToolCall，也不要求由
不同 Agent 执行；同一个 Agent 可以执行多个 Stage。拆分 Stage 必须同时满足两个条件：

1. 任务中存在两个或更多可以独立完成、独立返回结果的推理或执行单元，每个单元都值得启动
   一次独立 Agent Run；
2. Host 必须在这些 Run 之间建立至少一个以下实质边界：

   - **职责和控制边界**：应用必须固定执行顺序，不能把下一步完全交给模型动态决定；
   - **能力和治理边界**：Tool、Skill、权限、副作用确认策略或数据访问范围需要不同；
   - **上下文边界**：下一阶段不应继承上一阶段的完整消息和 Tool observation，只接收有限交接；
   - **验证检查点边界**：Host 验证结果必须成为后续独立 Run 的强制前置条件，或者形成可复用、
     可恢复的 Artifact 检查点；
   - **模型和预算边界**：阶段需要不同 Provider、模型、Token/ToolCall 预算或超时；
   - **失败和恢复边界**：需要独立取消、重试、人工确认、幂等检查点或从已完成阶段恢复；
   - **审计边界**：必须单独记录某项产物由哪个执行单元生成、验证和交接。

验证本身不自动产生 Stage。验证只是同一 Run 推理过程的一部分时，可以由 Agent 调用验证 Tool；
验证只约束单个 Run 的最终输出时，可以由 Harness 的结构化输出校验或终态 verifier 执行。只有
验证需要在两个独立 Run 之间形成 Host 强制门禁时，才属于 Stage 拆分依据。

以下情况不得仅为了形式上的“多步骤”拆 Stage：

- 相同 Agent、上下文、能力、权限、模型和失败语义下的一组动态 ToolCall，应留在一个
  model/tool loop 内；
- 一个确定性的机械步骤只调用一个 Tool 时，应优先由普通应用代码直接编排；
- 仅仅需要检查中间产物，且验证可以在同一 Run 内由确定性代码、Tool 或终态 verifier 完成，
  不构成 Stage 拆分理由；
- 仅仅更换 Stage Prompt 或使用不同 Agent 名称，不构成拆分理由；
- 如果无法说明阶段输出契约，以及为什么必须在该位置验证、隔离或恢复，则不应拆 Stage。

Stage 间只传递原始 Flow 输入和显式、有限、可验证的 Stage output/handoff。不得隐式继承上一个
Agent 的消息历史、Tool/Skill、权限、Provider 客户端或其他运行期对象。

### 4.2 Planner–ReAct Flow 不变量

PlannerReActFlow 是 Agent/Harness 上层的 Host 编排，不是新的 Agent Runtime。Planner 与
Executor 在 Flow 构造时固定；动态变化的是 Plan 和 Executor 在独立 Run 内选择的 ToolCall，
第一版不根据 Step 动态路由 Agent：

```text
PLANNING
   ↓
EXECUTING(one Step / one Agent Run)
   ↓
REVIEWING ── continue/revise ──> EXECUTING
   └────── complete/fail ──────> terminal
```

1. Planner Agent 必须返回 `PlannerDecision`，Executor Agent 必须返回 `StepOutcome`；模型不能
   直接修改 Flow PlanStore，也不能生成 `flow_run_id`、`plan_id`、`step_id` 或 `agent_run_id`；
2. 每次 Planner create/review 和 Executor Step 都使用 `Agent.start()` 创建独立普通 Run，保留
   原有 snapshot、Context、Memory、Tool/Skill、预算、Event 和取消语义；
3. `FlowChildRun` 是关联事实，显式保存 `flow_run_id`、`plan_id`、source/target revision、
   `step_id`、attempt 和 `agent_run_id`；不得从 Prompt、Message 或 Event 内容反向猜测关联；
4. Plan revision 表示 Planner 接受的一次结构变更。Review 只能保留剩余后缀或用全新的 Host id
   替换它；已经执行为 succeeded/failed 的终态前缀不可修改；
5. Agent Run 的 `RunStatus.SUCCEEDED` 只表示运行与结构化输出校验成功，Step 的业务成功由
   `StepOutcome.success` 单独表达；业务失败可以交给 Planner revise，Agent Run 失败直接使 Flow
   fail-closed；
6. Flow 有独立 `max_steps`、`max_revisions` 和 `max_child_runs`，不能依赖单个 Agent 的
   Model Turn/ToolCall 预算阻止跨 Run 循环；
7. Flow Event 只记录 `plan.*`、`planner.*`、`step.*` 和 `flow.*` 编排事实并引用
   `agent_run_id`；子 Run 的详细 `model.*`、`tool.*`、`message.*` Event 不复制到 Flow stream；
8. Flow 取消必须取消当前活动的子 Run，并闭合 `step.cancelled`/`planner.cancelled`、
   `plan.cancelled` 和 `flow.cancelled`；直接取消 `flow.run()` 的调用方 Task 仍向调用方传播
   `CancelledError`，后台 `FlowRunHandle.cancel()` 返回 cancelled 终态结果；
9. Planner 与 Executor 的 Memory scope 分开显式传入。Plan/Step/Outcome 是 Flow 领域状态，
   不作为 Message、Memory 或 Context 隐式共享；
10. 当前 FlowRunHandle、Plan 状态和子 Run 关系只支持单进程。多 Worker 时必须先持久化关联，
    再投递预分配 `agent_run_id` 的 Run，不能依靠进程内 Handle 补记关系。

### 4.3 Skill Package 不变量

1. 一个 Skill 一个目录，`SKILL.md` 是经验 playbook，不是 Tool 清单；
2. `dependencies` 表示外部 required/optional Tool，`contributions` 表示 Skill 自带能力；
3. required dependency 缺失时在模型请求前失败，optional dependency 缺失时允许降级；
4. permissions 与 capability 是否存在相互独立；
5. Skill-contributed Tool 自动进入 Agent，不要求应用重复注册；
6. `scripts/` 文件自动进入包清单，但目录扫描不会授予执行能力；
7. 外部 Skill root 使用只读 `discover()`，应用自己拥有的 root 可显式使用
   `discover_trusted()` 加载固定 `tools.py`；
8. 纯指令型 Skill 由 frontmatter 直接建立；普通代码型 Skill 的本地 `@tool` 自动进入
   统一 Tool 集，Script 默认只是 Tool 内部实现；
9. Skill 包指纹、依赖、贡献、权限和资源清单固定进 HarnessSnapshot。
10. `skill.selected` 记录选择事实，`skill.loaded` 记录模型通过 `skill_view` 实际加载 playbook；
   `skill.resource.used` 把 Skill 自有资源与 ToolCall 关联，
   Tool 的最终结果仍由 `tool.completed` 和 `ToolResult` 表达。

### 4.4 Event 不变量

1. EventStore 是唯一事实源，Harness 和 RunHandle 不各自保存 Event 副本；
2. `emit()` 原子分配 Run 内 sequence，并在 Observer 扇出前完成提交；
3. `list(after_sequence=N)` 和 `subscribe(after_sequence=N)` 只返回 sequence 大于 N 的事件；
4. Observer 是 fail-open 旁路，EventStore 写入不是 fail-open；
5. Redis 只能装饰 durable EventStore 提供通知，不能代替 PostgreSQL 等事实源。
6. Message 使用同一条 Event sequence：user `message.created` 和 assistant
   `message.completed` 始终存在，`message.delta` 只是可选实时增量。
7. `run.succeeded`、`run.failed` 和 `run.cancelled` 是互斥的 Run 终态 Event；取消期间已经
   `tool.started` 的调用必须以 `tool.completed(status=cancelled)` 闭合。
8. user/assistant `message.*` Event 的 `message_id` 与 Harness transcript、后续 ModelRequest
   中对应的 `Message.message_id` 相同；Tool Message 使用独立 id，并以 `tool_call_id` 关联调用。

### 4.5 Context 不变量

`HarnessSnapshot.system_prompt` 和 `messages` 分别是 Run 启动时冻结的完整 system prompt 与
Run 内形成的原始 transcript；`ContextView.system_prompt/messages` 是一次模型请求实际使用的
临时投影。source 与 request view 不能混为同一份可变状态：

```text
SystemContext + transcript + MemorySnapshot + request overhead + ContextBudget
                    │
                    ▼
             ContextPolicy.select()
                    │
                    ▼
 ContextView(system_prompt, messages, ContextTrace)
                    │
                    ▼
              ModelRequest.messages
```

1. 默认 `KeepAllContextPolicy` 保持原有行为，不宣称执行 token 裁剪；
2. ContextPolicy 是同步、确定性的纯选择边界，不能自行执行网络召回、模型摘要或持久化写入；
3. SystemContext 由不可修改的核心 instructions 和声明式、应用可信的 SystemContextBlock 组成；
   required block 必须保留，optional block 可由 Policy 按预算选择；
4. ContextView.system_prompt 必须严格由核心 instructions 和 trace 中选中的已声明 blocks 渲染，
   Policy 不能自由改写核心指令，也不能把 Memory 或任意文本临时提升为 system 权限；
5. `ContextInput.transcript` 使用不可变 tuple，策略只能返回新的 request-only ContextView；
6. ContextView 必须保留 user Message，并保持 assistant ToolCall 与其所有 Tool result 为完整组；
7. 策略异常、无效 system selection、无效角色、孤立 Tool result、缺失 Tool result 或违反已报告预算时 fail-closed，
   不能回退为完整历史；
8. `ContextBudget` 描述 context window、输出预留和安全余量，具体 token 估算与执行由选中的
   Policy 负责，因为 tokenizer 属于 Model/Provider 能力；
9. `context.prepared` 记录 source/selected system prompt 指纹、selected block ids、Policy、
   消息计数、裁剪计数和 token 估算，不记录上下文正文；
10. ContextPolicy、SystemContext block 清单和预算固定进 HarnessSnapshot 与 fingerprint；
11. MemoryProvider 每个 Run 最多 recall 一次，同一份不可变 MemorySnapshot 被该 Run 的所有
   Model Turn 复用；ContextPolicy 必须显式报告 Memory candidate 和 selected ids；
12. Memory recall 是 fail-open 的旁路，Provider 失败产生 `memory.recall.failed` 并使用空的
   enabled MemorySnapshot；recall 有固定超时，且不能回退到其他 tenant/user scope；
13. 启用 Memory read 或 write 必须由应用给 Run 传入显式 `MemoryScope`，HTTP body 不接受
   客户端自报 scope；
14. MemoryWriter 只提供同步、非阻塞 `enqueue()` 边界，Harness 在成功终态提交完整 Message
   transcript；实际抽取、合并、去重、后台 I/O 和 Redis/PG 实现后续补充；
15. Memory、摘要器和 ContextPolicy 不合并为一个接口。Memory 只提供 typed candidates，
   ContextPolicy 决定哪些内容进入本次 request-only Message 投影。

`ModelRequest` 不增加 `memory`、`context_policy` 或 `context_trace` 字段。Provider 只接收已经
准备好的 `ContextView.system_prompt/messages`、Tools 和 response schema；选择过程由 Context
Event 单独观测。

完整契约见 [`SKILLS.md`](SKILLS.md)。
Event 存储和流契约见 [`EVENTS.md`](EVENTS.md)。

## 5. Model Turn 与 ToolCall 预算

`AgentDefinition` 使用两个相互独立、含义明确的限制：

```python
AgentDefinition(
    ...,
    max_model_turns=3,
    max_tool_calls_per_turn=3,
)
```

- 一个 Model Turn 等于一次 `ModelProvider.complete()`，或一个以
  `ModelStreamCompleted` 结束的完整 `StreamingModelProvider.stream()`；
- 最后一个 Model Turn 固定用于 finalization，不向模型提供 Tool；
- 前面的每个 Model Turn 最多执行 `max_tool_calls_per_turn` 个 ToolCall；
- 模型单轮返回更多 ToolCall 时，只执行前 N 个；
- 其余 ToolCall 生成 `limit_exceeded` ToolResult 和 Observation，不静默丢弃；
- 因此下一轮请求仍为模型返回的每个 `tool_call_id` 提供完整 Tool 消息；
- 一次 Run 最多实际执行
  `(max_model_turns - 1) * max_tool_calls_per_turn` 个 ToolCall。

`tool_choice="required"` 表示整个 Run 至少产生一次 ToolCall，而不是要求每次模型请求
都必须调用 Tool。第一次 ToolCall 后，后续普通 Turn 使用 `auto`，finalization Turn
固定使用 `none`。

这两个限制及其具体值进入 `HarnessSnapshot` 和 fingerprint，确保预算变化可以被审计。

## 6. 后续迁移顺序

1. 已增加进程内 RunHandle、`start()`、Event 回放与实时订阅，以及 `base_agent.server`
   HTTP Run 提交/状态查询，以及基于同一 EventStore 的 JSON 回放与 SSE 实时订阅；
2. 已增加进程内 `RunHandle.cancel()`、HTTP cancel 和 `run.cancelled` 终态语义；
3. 已增加小型 ContextPolicy、默认 KeepAll 策略、ContextBudget 和 request-only
   ContextView，以及默认禁用的 scoped Memory recall/non-blocking write 契约；
4. 已增加固定角色、Host-owned Plan 的 PlannerReActFlow，使用 `PlannerDecision` 和
   `StepOutcome` 验证逐步执行、Review/Replan、实时事件、子 Run 关联和取消传播；并通过
   独立 `/v1/flows` HTTP API 提供提交、查询、取消、回放和 SSE；
5. 根据 PlannerReActFlow 的真实 StepOutcome 和后续 AgentDemo MTBI 场景，再判断是否提取
   SequentialFlow 共用的声明式 Stage/Artifact output contract；
6. 已建立独立 `mtbi-analysis` 下游应用，用 AgentDemo 的固定查询/分析场景离线验证
   SequentialFlow handoff、SQL/CSV/Markdown 产物和 Host verifier；下一步使用真实 Provider 与
   `mtbi-cli` 验收，再决定是否提取公共 Artifact/StageOutput 契约；
7. 在已有 Skill 受控目录发现、Descriptor/Catalog/`skill_view` 基础上，根据真实
   目录规模再增加检索、缓存和外部 Skill 源 Adapter；
8. 在已有 EventStore/EventStream 小型契约上按真实部署需求增加 PostgreSQL durable store
   和 Redis notification adapter，再迁入 Artifact、Resource 等其他 Protocol。

Worker、Lease、Operator API 和分布式恢复不在以上主路径内；只有明确部署需求出现时，才
作为独立生产 Adapter 评估。

### 多进程 Run 管理（已记录，暂缓实现）

当前 `InMemoryRunRegistry`、`InMemoryFlowRegistry`、`RunHandle` 和 `FlowRunHandle` 都是执行
进程内对象，只支持单 Worker。以后支持多进程或多实例时，HTTP 层不能再把 Handle 当权威
记录：

1. 共享 `RunStore` 保存 Run 状态、结果、取消标记和执行租约；
2. API Worker 只创建/查询 RunRecord，不直接持有后台 `asyncio.Task`；
3. Execution Worker 领取 Run 后，可以在本进程内部使用 `RunHandle`，但它只是执行细节；
4. PostgreSQL EventStore 作为事件事实源，Redis/PgNotify 只负责实时唤醒 SSE；
5. Worker 崩溃后的重新领取依赖 lease、幂等写入和后续 checkpoint，而不是恢复旧 RunHandle。

已经明确的部署门槛：`RunHandle` 只保留为 Execution Worker 内部控制本地 Task 的原语；任何
Kubernetes、多 Pod、多 Worker、滚动更新或弹性扩缩容部署，都必须先把 HTTP Run 创建、查询、
取消和结果读取迁移为共享 `RunStore`/`FlowRunStore` 驱动。API Pod 通过 Store 写入取消请求，
持有 lease 的 Worker 再取消自己的本地 Handle；负载均衡、sticky session 或共享 EventStore
都不能替代状态 Store。

这部分等单进程 Run/Event/Server 契约稳定后再实现，当前 Starter 明确保持单 Worker，不能
作为多副本 K8s Run Server 部署。
