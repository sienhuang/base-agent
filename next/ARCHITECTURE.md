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
| `server/` | 可选 FastAPI Run 提交、状态查询和进程内 Handle registry | 否 |
| `flows/` | Stage 和简单 SequentialFlow | 是 |
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
| mock-manus | model/tool 闭环、有序执行事件、Planner/Executor 的后续预设方向 |
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

Skill Package 的额外不变量：

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

Event 的额外不变量：

1. EventStore 是唯一事实源，Harness 和 RunHandle 不各自保存 Event 副本；
2. `emit()` 原子分配 Run 内 sequence，并在 Observer 扇出前完成提交；
3. `list(after_sequence=N)` 和 `subscribe(after_sequence=N)` 只返回 sequence 大于 N 的事件；
4. Observer 是 fail-open 旁路，EventStore 写入不是 fail-open；
5. Redis 只能装饰 durable EventStore 提供通知，不能代替 PostgreSQL 等事实源。
6. Message 使用同一条 Event sequence：user `message.created` 和 assistant
   `message.completed` 始终存在，`message.delta` 只是可选实时增量。

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
2. 增加取消语义和小型 ContextPolicy；
3. 增加声明式 Stage output contract；
4. 用 AgentDemo MTBI Flow 验证 Artifact/Verifier；
5. 增加固定角色的 Planner/Executor Flow 预设；
6. 用 mock-manus PlannerReActFlow 验证重新规划和实时事件；
7. 在已有 Skill 受控目录发现、Descriptor/Catalog/`skill_view` 基础上，根据真实
   目录规模再增加检索、缓存和外部 Skill 源 Adapter；
8. 在已有 EventStore/EventStream 小型契约上按真实部署需求增加 PostgreSQL durable store
   和 Redis notification adapter，再迁入 Artifact、Resource 等其他 Protocol。

Worker、Lease、Operator API 和分布式恢复不在以上主路径内；只有明确部署需求出现时，才
作为独立生产 Adapter 评估。

### 多进程 Run 管理（已记录，暂缓实现）

当前 `InMemoryRunRegistry` 和 `RunHandle` 都是执行进程内对象，只支持单 Worker。以后支持
多进程或多实例时，HTTP 层不能再把 `RunHandle` 当权威 Run 记录：

1. 共享 `RunStore` 保存 Run 状态、结果、取消标记和执行租约；
2. API Worker 只创建/查询 RunRecord，不直接持有后台 `asyncio.Task`；
3. Execution Worker 领取 Run 后，可以在本进程内部使用 `RunHandle`，但它只是执行细节；
4. PostgreSQL EventStore 作为事件事实源，Redis/PgNotify 只负责实时唤醒 SSE；
5. Worker 崩溃后的重新领取依赖 lease、幂等写入和后续 checkpoint，而不是恢复旧 RunHandle。

这部分等单进程 Run/Event/Server 契约稳定后再实现，当前 Starter 明确保持单 Worker。
