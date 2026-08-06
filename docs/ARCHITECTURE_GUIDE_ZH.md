# base-agent 中文架构与扩展指南

这份文档面向两类读者：

- 希望从整体上理解 `base-agent`，不再逐个函数猜测用途的学习者；
- 希望基于现有边界增加 Provider、Tool、Skill、存储、浏览器、沙箱或业务能力的开发者。

它描述当前仓库已经实现的代码，而不是一个脱离实现的目标架构。更细的专题说明仍以
[`docs/`](.) 下的英文文档和对应测试为准。

## 1. 先建立一个最小心智模型

`base-agent` 不是一个包含所有能力的“大 Agent 类”。它是一个异步 Agent 运行时，
负责把以下对象组合成一次可观察、可限制、可暂停和可恢复的 Run：

```text
AgentDefinition    版本化业务定义：指令、能力名单、权限和预算
ModelProvider      模型边界：ModelRequest -> ModelResponse
Tool               原子能力：结构化参数 -> 结构化结果
Skill              版本化流程说明：允许哪些 Tool、需要哪些权限
Supervisor         运行保护：预算、重复调用、连续失败
Runtime            生命周期：状态、循环、事件、取消、等待和恢复
Store              持久化端口：Run、Event、Checkpoint、Artifact
Resource           执行期基础设施：数据库会话、浏览器、沙箱、MCP 客户端
```

最常见的执行路径是：

```text
用户输入
  │
  ▼
Agent.run()
  │  选择 Skill、确认 Attachment、计算本次可用 Tool
  ▼
AgentRuntime
  │  创建 RuntimeContext、Run、事件和执行期 Manager
  ▼
ModelToolStrategy
  │  组装 ModelRequest
  ▼
ModelProvider
  │
  ├── 返回普通文本 ───────────────► Run COMPLETED
  │
  └── 返回 ToolCall
          │
          ▼
      Supervisor
          │
          ▼
      ToolExecutor
          │  检查启用名单、注册、权限、参数和超时
          ▼
      Tool / ContextualTool
          │
          ├── 返回 ToolResult ─────► 追加 tool 消息，进入下一轮模型调用
          └── 返回 WaitForInput ───► 保存 Checkpoint，Run WAITING
```

只记住一句话：

> Agent 是组合入口，Runtime 管生命周期，Strategy 管一轮怎么走，Provider 调模型，
> Tool 做原子动作，Skill 描述可复用流程，Store 和 Resource 接入基础设施。

## 2. 项目的边界

### 2.1 它是什么

`base-agent` 是一个框架无关的 Python 核心库，提供：

- Provider 中立的模型请求和响应；
- 类型化 Tool 及运行时校验；
- 版本化 Skill；
- 默认的 Model → Tool → Model 循环；
- 可替换的编排策略；
- Run 状态、事件、后台执行、取消、等待和恢复；
- 执行预算与无进展保护；
- Resource、Artifact、Memory 等通用能力端口；
- 内存实现和若干可选基础设施适配器。

它首先面向 Agent 应用开发者，而不是只面向 Runtime 或基础设施开发者。首要产品目标是：

> 让 Agent 开发者专注编写 Prompt、Tool、Skill 和少量 Agent/Flow 编排策略，通过组合
> 而不是开发 Runtime，就能快速构建一个可运行、可测试、可扩展的单 Agent 或简单
> 多 Agent 应用。

Harness、Run、Event、Checkpoint、Store、Worker 和治理能力是框架为实现这个目标而吸收
的工程复杂度，不应成为普通 Agent 开发者必须理解和重复实现的前置知识。

### 2.2 它不是什么

核心包本身不是：

- Web 产品或聊天 UI；
- 多租户鉴权系统；
- 分布式任务队列；
- 跨进程工作调度器；
- 向量数据库；
- 通用文件解析系统；
- 业务 Agent 或业务工作流；
- 默认开放宿主机权限的代码执行平台；
- 内置多 Agent 协作框架。

FastAPI、PostgreSQL、Redis、MCP、Docker、Playwright 和 OpenAI SDK 都位于可选边界。
业务应用依赖 `base-agent`，核心包不得反向导入业务代码。

## 3. 分层与依赖方向

仓库采用“核心模型和协议稳定、实现从外部注入”的结构：

```text
应用组合层
starter / 业务应用 / CLI / HTTP 服务
                │
                ▼
公共门面层
Agent / AgentDefinition / AgentProfile / RunHandle
                │
                ▼
运行时与编排层
AgentRuntime / RuntimeContext / OrchestrationStrategy / Supervisor
                │
                ▼
核心契约层
ModelProvider / Tool / Store / Resource / Memory / Browser / Sandbox Protocol
                │
                ▼
实现与适配器层
内存实现 / OpenAI / PostgreSQL / Redis / MCP / Docker / Playwright
```

依赖规则：

1. Runtime 可以依赖核心模型和 Protocol。
2. 可选适配器可以依赖核心 Protocol。
3. 核心不能依赖 FastAPI、数据库、Docker、浏览器或具体业务。
4. Tool 和 Skill 不应该直接创建全局基础设施客户端。
5. 活的连接、容器和页面对象不能进入消息、Run 或 Checkpoint。

### 3.1 从 Harness Engineering 视角理解 base-agent

在 LLM Agent 语境中，Harness 不是某一个类，也不只是 Model → Tool → Model 循环。它是
围绕模型、让 Agent 能够可靠执行的整套运行框架，包括执行环境、Tool、上下文、编排、
可观察性、验证和治理。

从这个视角看，`base-agent` 当前已经是一个正在成形的 Agent Harness：

- `AgentRuntime` 是生命周期内核，统一控制 Run 状态、模型/Tool 迭代、取消、等待、恢复
  和终态提交；
- `OrchestrationStrategy` 决定一次有界执行如何推进，但不拥有完整 Run 生命周期；
- Tool、Resource、Sandbox 和 Browser 决定 Agent 能够在什么环境中执行哪些动作；
- Message、Skill、Memory、Attachment 和 Artifact 共同构成模型看到和执行过程中保留的
  上下文；
- Event、日志、Store 和 SSE 使执行过程可观察、可回放；
- Supervisor、权限和资源边界提供当前已有的运行控制与部分治理能力。

可以把产品定位概括为：

> 一个面向 Agent 开发者、规范驱动、可观察、可验证、可治理的 Agent Harness 与任务
> 执行内核；它隐藏通用 Runtime 复杂度，并提供受控的简单多 Agent 编排。

其中各部分有明确的主次关系：

1. **开发者价值是首要目标**：开发者主要编写 Prompt、Tool、Skill、Agent 和少量 Flow；
2. **组合是主要扩展方式**：通过 Profile、Definition、Toolkit、Skill、Policy、Strategy
   和 Adapter 组合能力，不要求复制 Runtime 或建立深继承树；
3. **Harness 是技术底座**：统一吸收生命周期、状态、上下文、执行环境、观察、验证和
   治理复杂度；
4. **任务执行内核提供可靠性**：Run、Event、Checkpoint、取消、等待、恢复和持久化采用
   一致语义；
5. **多 Agent 保持受控和简单**：支持命名 Agent、固定 Flow、路由和
   Planner/Executor 分工，但不演进为自治 Agent 社会或通用分布式工作流平台。

因此，后续设计不应在 `AgentRuntime` 旁边再创建一套重复的“大 Harness Runtime”。更合理
的方向是把现有执行内核明确视为 **Harness Kernel**，继续通过小型 Protocol、Policy 和
Adapter 补齐缺失能力。`Agent` 仍然是面向应用开发者的简单组合入口，不要求普通开发者
理解 Harness 内部细节。

这里的 Harness 与 `testing/` 中的 `ToolHarness`、`SkillHarness` 含义不同：

- **Agent Harness / Harness Kernel**：生产执行所依赖的完整运行框架；
- **Testing Harness**：隔离测试 Tool、Skill、Agent 或 Flow 的测试工具。

### 3.2 ETCLOVG 七层映射

可以使用 ETCLOVG 分类检查 Harness 是否完整。该分类不是新的包结构要求，而是跨现有模块
的架构视图：

| 层 | 关注点 | 当前对应模块与能力 | 当前判断 |
| --- | --- | --- | --- |
| E — Execution environment | Agent 在哪里运行、资源如何隔离和回收 | `resources/`、`sandbox/`、`browser/` | 已有执行段资源生命周期和可选隔离环境；生产网络、文件和资源策略仍需强化 |
| T — Tool interface | Tool schema、调用协议、权限、超时和结果边界 | `tools/`、`toolkits/`、`mcp/`、`data_sources/` | 已有副作用分类、确认、Flow 账本、幂等键注入和统一结果上限 |
| C — Context management | 模型每一步看到什么，记忆和历史如何选择、压缩 | `RuntimeContext`、Message、Skill、Memory、Attachment、Artifact | 已有结构化来源；尚缺统一 Context budget、选择和 compaction 契约 |
| L — Lifecycle / Orchestration | Run、Plan、ReAct、取消、等待、恢复和故障语义 | `runtime/`、`orchestration/`、`RunHandle`、Checkpoint | 当前最成熟；仍缺进程重启后的 durable executor、lease 和恢复 |
| O — Observability | 执行是否可检查、关联、回放、度量和追踪 | Runtime Event、日志、PostgreSQL、Redis、SSE、TokenUsage | 已有有序事实和回放；仍缺统一 Metrics、Trace 和安全的 Event payload policy |
| V — Verification | 运行前是否就绪，结果和轨迹是否正确可信 | 结构化模型、测试、部分 Supervisor 策略 | 当前明显不足；尚无独立 Verifier、trajectory evaluation 和 trace-to-regression 契约 |
| G — Governance | 身份、租户、权限、副作用、安全、审计和保留 | Profile permissions、ToolExecutor、Supervisor、Sandbox 边界 | 有最小权限基础；缺租户授权、确认、数据策略、保留和完整审计 |

前四层决定 Agent 如何执行，O/V/G 是贯穿所有执行层的控制面。Observability、Verification
和 Governance 不应被隐藏成某个 Strategy 的内部回调，因为它们需要独立的契约、配置、
事件、测试和生产运维责任。

特别需要区分：

- **Supervisor** 回答“这一步是否允许继续、是否超过预算、是否陷入无进展循环”；
- **Verifier** 回答“环境是否就绪、最终结果是否正确、执行轨迹是否可信”；
- **Governance Policy** 回答“谁有权执行或读取、哪些副作用需要确认、数据如何审计和
  保留”。

当前 Supervisor 不能代替 Verification 或完整 Governance。

### 3.3 AgentDemo、mock-manus 与 base-agent 的关系

`AgentDemo` 和 `mock-manus` 都已经包含项目内的隐式 Harness，但它们应作为参考实现和
下游消费者，而不是核心依赖：

```text
AgentDemo ────┐
mock-manus ───┼──> base-agent Harness Kernel
业务 Agent ───┘
```

从 `AgentDemo` 提炼的重点包括有界步骤循环、无进展检测、可替换的 ReAct/Planning、
规范化 Tool 集合和可选 Memory。从 `mock-manus` 提炼的重点包括后台 Task、类型化事件、
Planner/ReAct Flow、Sandbox/Browser 生命周期、Attachment/Artifact 和人工等待。

抽取原则是：

1. 通用生命周期、状态、事件和安全边界进入 `base-agent`；
2. 业务 Prompt、角色、UI、会话产品语义和领域 Tool 留在应用；
3. 不直接复制同时构造 LLM、数据库、Sandbox、Browser 和 Flow 的应用级大类；
4. 每项抽取能力必须能使用内存实现和 FakeModel 做确定性测试；
5. `base-agent` 不导入或暴露以 AgentDemo、mock-manus 命名的类型。

详细的已采纳模式和明确不复制的模式见
[`REFERENCE_DESIGN.md`](REFERENCE_DESIGN.md)。

### 3.4 Harness 配置快照与可复现性

同一个模型在不同 Tool schema、Context policy、Sandbox、重试规则或 Verifier 下可能产生
完全不同的结果，这称为 Harness coupling。一次评估实际测量的是
**model–harness–environment** 组合，而不是模型本身。

目标架构应逐步引入声明式、不可变的 Harness 配置，例如：

```python
HarnessSpec(
    execution=ExecutionPolicy(...),
    tools=ToolPolicy(...),
    context=ContextPolicy(...),
    lifecycle=LifecyclePolicy(...),
    observability=ObservabilityPolicy(...),
    verification=VerificationPolicy(...),
    governance=GovernancePolicy(...),
)
```

`HarnessSpec` 是目标概念，当前尚未成为公共 API。它不负责重新实现 Runtime，而是组合七层
已有或待实现的 Policy 和 Adapter。普通应用应能选择安全默认配置，而不是逐项构造全部
策略。

每个 Run 还应记录不可变的 `HarnessSnapshot` 或等价审计信封，至少包含：

- Provider、模型和执行环境版本；
- 已启用 Tool 的名称、schema、版本、权限和副作用分类；
- Context budget、检索和压缩策略；
- Strategy、超时、重试、Checkpoint 和恢复策略；
- Event payload、Verifier 和 Governance policy 版本；
- 整体配置 hash。

`HarnessSnapshot` 同样属于目标设计，当前 Run 尚未完整保存这些信息。引入它的目的不是让
模型看到更多配置，而是支持问题归因、运行复现、基准比较、审计和回归测试。目标架构及
实施顺序以 [`ARCHITECTURE_DESIGN_ZH.md`](ARCHITECTURE_DESIGN_ZH.md) 和
[`TODO.md`](TODO.md) 为准。

## 4. 目录和文件职责

### 4.1 顶层

| 路径 | 作用 |
| --- | --- |
| [`README.md`](../README.md) | 项目入口、安装和文档导航。 |
| [`pyproject.toml`](../pyproject.toml) | 核心依赖、可选 extra、测试和静态检查配置。 |
| [`examples/`](../examples) | 最小离线示例，适合验证概念。 |
| [`starter/`](../starter) | 可复制的完整应用模板。 |
| [`tests/`](../tests) | 行为契约和适配器测试，也是最可信的使用示例。 |
| [`docs/`](.) | 架构、能力边界和部署说明。 |

### 4.2 核心包

| 模块 | 主要职责 | 建议先读的文件 |
| --- | --- | --- |
| `agent.py` | 公共门面；组合依赖；启动、恢复、取消 Run。 | [`agent.py`](../src/base_agent/agent.py) |
| `profiles.py` | Agent 的静态能力、权限和预算。 | [`profiles.py`](../src/base_agent/profiles.py) |
| `models/` | Provider 中立、可序列化的数据契约。 | [`model.py`](../src/base_agent/models/model.py)、[`run.py`](../src/base_agent/models/run.py) |
| `runtime/` | 生命周期、状态机、Checkpoint 和 Run 快照。 | [`engine.py`](../src/base_agent/runtime/engine.py) |
| `orchestration/` | 一轮执行策略、默认模型工具循环和计划更新。 | [`model_tool.py`](../src/base_agent/orchestration/model_tool.py) |
| `flows/` | FlowDefinition、显式 Handoff 和 AgentInvoker 调用边界。 | [`models.py`](../src/base_agent/flows/models.py) |
| `tools/` | Tool Protocol、装饰器、注册表、执行器和 ToolContext。 | [`decorator.py`](../src/base_agent/tools/decorator.py)、[`executor.py`](../src/base_agent/tools/executor.py) |
| `skills/` | Skill Manifest、延迟加载、注册和校验。 | [`loader.py`](../src/base_agent/skills/loader.py)、[`validator.py`](../src/base_agent/skills/validator.py) |
| `supervision/` | 模型和工具调用前后的运行保护策略。 | [`policies.py`](../src/base_agent/supervision/policies.py) |
| `resources/` | 执行期资源获取、缓存和逆序释放。 | [`manager.py`](../src/base_agent/resources/manager.py) |
| `artifacts/` | 当前 Run 对 Attachment/Artifact 内容的受控访问。 | [`manager.py`](../src/base_agent/artifacts/manager.py) |
| `memory/` | 可选检索契约、初始化和 Tool 查询入口。 | [`manager.py`](../src/base_agent/memory/manager.py) |
| `stores/` | Store Protocol、内存默认实现及可选持久化适配器。 | [`protocol.py`](../src/base_agent/stores/protocol.py)、[`in_memory.py`](../src/base_agent/stores/in_memory.py) |
| `providers/` | ModelProvider Protocol 和模型厂商适配器。 | [`protocol.py`](../src/base_agent/providers/protocol.py) |
| `toolkits/` | 基础 Tool 工厂以及具体 Coding 组合。 | [`bundle.py`](../src/base_agent/toolkits/bundle.py)、[`coding.py`](../src/base_agent/toolkits/coding.py) |
| `web_search/` | Web Search Provider、结果模型、Tool 和 Brave 适配器。 | [`protocol.py`](../src/base_agent/web_search/protocol.py) |
| `data_sources/` | 只读数据源端口、查询 Tool、Artifact 溢出和 MTBI CLI / OneSQL 适配器。 | [`protocol.py`](../src/base_agent/data_sources/protocol.py) |
| `testing/` | FakeModel、ToolHarness、SkillHarness、AgentTestHarness。 | [`harness.py`](../src/base_agent/testing/harness.py) |

### 4.3 可选适配器

| 模块 | 能力 | 安装 extra |
| --- | --- | --- |
| `providers/openai_chat.py` | OpenAI-compatible Chat Completions | `base-agent[openai]` |
| `server/` | FastAPI HTTP 与 SSE 映射 | `base-agent[server]` |
| `stores/postgres/` | Run/Event/Checkpoint/Artifact 持久化 | `base-agent[postgres]` |
| `stores/redis/` | 持久化 EventStore 上的低延迟 Pub/Sub 通知 | `base-agent[redis]` |
| `mcp/` | MCP Tool 发现和调用 | `base-agent[mcp]` |
| `sandbox/` | Sandbox Protocol、Tool 和 Docker 实现 | `base-agent[sandbox]` |
| `browser/` | Browser Protocol、Tool 和 Playwright 实现 | `base-agent[browser]` |

## 5. 核心对象之间的关系

### 5.1 AgentDefinition：版本化业务定义

[`AgentDefinition`](../src/base_agent/profiles.py) 描述一个 Agent“是谁、能做什么、受到哪些
限制”。它是不可变、可版本化的应用定义：

```python
definition = AgentDefinition(
    id="order-agent",
    version="1.0.0",
    instructions="Help with orders.",
    model="model-route",
    tools=("get_order",),
    skills=("order-analysis",),
    permissions=frozenset({"orders:read"}),
    max_steps=10,
    max_tool_calls=50,
)
```

`definition.fingerprint` 是定义内容的稳定 SHA-256，用于变更识别，并为后续
HarnessSnapshot、Flow compatibility 和运行审计提供基础。它不是 Secret，也不会自动进入
模型 Prompt。

具体 Provider、Tool 实现、Store、Resource、Supervisor 和 Runtime 不进入
`AgentDefinition`，仍由应用 composition root 注入：

```python
agent = Agent(
    definition=definition,
    model=provider,
    tools=tools,
    skill_registry=skills,
    resources=resources,
)
```

`AgentProfile` 是向后兼容的 Runtime 配置类型。现有应用可以继续使用
`Agent(profile=AgentProfile(...))`；新应用优先使用
`Agent(definition=AgentDefinition(...))`。二者必须且只能提供一个。当前 Runtime 和
Checkpoint 仍消费从 Definition 投影得到的 AgentProfile，因此此变更不修改既有执行和
恢复语义。

需要区分三个概念：

```text
Agent(tools=...)          注册：应用里有哪些 Tool 实现
definition.tools          启用：当前 Agent 可以向模型暴露哪些 Tool
definition.permissions    授权：执行器允许当前 Agent 使用哪些权限
```

注册不等于启用，启用也不等于授权。这个区分用于避免管理类或高风险 Tool 因为“已经注册”
就自动暴露或获得权限。

starter 可以从一个明确的 `ENABLED_TOOLS` 集合推导 `profile.tools`，减少重复名称；但不应
从 Tool 的权限要求自动推导 `profile.permissions`，否则注册高权限 Tool 会等同于自动授权。

### 5.2 Agent：公共组合门面

[`Agent`](../src/base_agent/agent.py) 不实现模型推理逻辑。它负责：

- 接收 Profile、Provider、Tool、Store、Supervisor、Resource 和 Memory；
- 建立 ToolRegistry 并确认 Profile 中的 Tool 已注册；
- 校验所选 Skill 和 Attachment；
- 调用 Runtime 创建并执行 Context；
- 暴露 `run()`、`start()`、`resume()`、`cancel()` 和 Artifact 操作。

它采用组合而不是继承。业务应用通常不需要创建 `OrderAgent(Agent)` 这样的深继承树，
而是在一个 composition root 中构造：

```python
agent = Agent(
    definition=definition,
    model=provider,
    tools=tools,
    skill_registry=skills,
    resources=resources,
    run_store=run_store,
    event_store=event_store,
)
```

### 5.3 RuntimeContext：一次 Run 的可变内存状态

[`RuntimeContext`](../src/base_agent/runtime/context.py) 只属于一个 Run，保存：

- 当前消息和模型响应；
- 已选 Skill 和可用 Tool 名单；
- step/tool 计数和 TokenUsage；
- 当前状态、输出和错误；
- Supervisor 状态；
- PendingInput 和 ExecutionPlan；
- Attachment/Artifact 引用；
- Memory 结果；
- Resource 清理失败信息。

它是执行期可变对象，不应该跨 Run 共享。

### 5.4 Run、Event、Checkpoint 和 AgentResult

这四种对象用途不同：

| 对象 | 用途 |
| --- | --- |
| `Run` | 当前持久化快照，适合查询“现在是什么状态”。 |
| `RuntimeEvent` | 按 sequence 排序的不可变历史事实，适合审计、回放和 SSE。 |
| `RuntimeCheckpoint` | WAITING 时恢复同一 Run 所需的完整可序列化状态。 |
| `AgentResult` | 一次 `run()` 或 `resume()` 返回给调用方的终态结果。 |

不要把 Event 当作 Run，不要把 Run 当作完整历史，也不要把活的资源放进 Checkpoint。

## 6. 默认执行链路

### 6.1 Agent 创建 Context

`Agent.run()` 首先：

1. 选择并校验显式指定的 Skill；
2. 根据 Profile 与 Skill allowlist 计算本次 enabled Tool；
3. 确认 Attachment 引用真实存在且内容引用一致；
4. 调用 `AgentRuntime.create_context()`。

Runtime 初始消息为：

```text
system: Profile instructions + 已选 Skill instructions
user:   本次 prompt
```

### 6.2 Runtime 建立执行期服务

`AgentRuntime.execute()` 创建或选择：

- RunStore、EventStore、CheckpointStore；
- Supervisor；
- ResourceManager；
- ArtifactManager；
- MemoryManager；
- RuntimeServices。

然后创建 Run、发出生命周期事件、启动 Resource、初始化 Memory，并进入：

```python
while context.state is RunStatus.RUNNING:
    await strategy.advance(context, services)
```

Runtime 自己拥有状态、取消、清理和终态构建；Strategy 只推进一个有界回合。

### 6.3 默认 ModelToolStrategy

[`ModelToolStrategy`](../src/base_agent/orchestration/model_tool.py) 的一次 `advance()`：

1. step 计数加一；
2. 从 ToolRegistry 取得 enabled ToolDefinition；
3. 用消息、Tool、模型路由、Attachment 和 Memory 创建 `ModelRequest`；
4. 发出 `model.requested`；
5. 调用 `provider.complete(request)`；
6. 保存响应、TokenUsage 和 assistant 消息；
7. 没有 ToolCall 时完成 Run；
8. 有 ToolCall 时逐个进入 Supervisor 和 ToolExecutor；
9. 把非 WAITING ToolResult 作为 `role="tool"` 消息加入上下文；
10. 返回 Runtime 循环，由下一轮再次调用模型。

### 6.4 Tool 执行链

[`ToolExecutor`](../src/base_agent/tools/executor.py) 按顺序检查：

```text
Tool 是否在本次 allowed_tools 中
        ↓
Tool 是否已注册
        ↓
Tool 所需权限是否都包含在 granted_permissions 中
        ↓
复制参数
        ↓
有 ContextualTool 能力时注入 ToolContext
        ↓
在 timeout 范围内调用
        ↓
规范化 SUCCESS / INVALID_ARGUMENTS / DENIED / TIMEOUT / ERROR / WAITING
```

`@tool(permissions=...)` 只声明权限要求；真正的权限验证在 ToolExecutor。直接调用
`FunctionTool.invoke()` 不经过这层授权检查，因此应用代码应通过 Agent/Runtime 或 Harness
执行 Tool。

### 6.5 Tool Result 回到模型

默认策略会执行：

```python
Message.tool(
    result.model_dump_json(),
    tool_call_id=call.id,
)
```

因此 ToolResult 会在下一轮模型请求中出现。失败结果通常也会回到模型，让模型改变策略或
解释失败；`WAITING` 是例外，它会暂停 Run。

大结果不能直接返回。`BoundedToolResultPolicy` 现在会在统一 ToolExecutor 边界测量完整
ToolResult JSON 的 UTF-8 字节数；默认上限 262,144 字节，并可通过
`AgentDefinition.max_tool_result_bytes` 固定。超限时原数据被替换为
`tool_result_too_large`，只保留原大小、上限、原状态和 rejected 动作。它不截断 JSON，
因此模型消息、Event 和后续 Checkpoint 看到的都是有效且有界的结果。

Runtime 不自动把未知大结果写成 Artifact，因为通用层无法判断敏感性、media type、访问权和
保留策略。Tool 应通过 `ToolContext.artifacts` 显式保存获准的 bulk data，再返回有界引用。

## 7. Tool 架构

### 7.1 Tool Protocol

[`Tool`](../src/base_agent/tools/protocol.py) 要求对象提供：

- `definition: ToolDefinition`
- `permissions: frozenset[str]`
- `timeout_seconds: float`
- `invoke(arguments)`

它是 `Protocol`，具体实现不必继承，只需结构匹配。`@runtime_checkable` 允许有限的
`isinstance()` 能力检测；完整签名仍由 mypy 检查。

### 7.2 @tool 装饰器

[`@tool`](../src/base_agent/tools/decorator.py) 把同步或异步 Python 函数包装成
`FunctionTool`：

```python
@tool(permissions=frozenset({"orders:read"}), timeout_seconds=10)
async def get_order(order_id: str) -> dict[str, str]:
    return {"order_id": order_id, "status": "paid"}
```

装饰器通过 `inspect.signature()` 和 `get_type_hints()` 动态创建 Pydantic 参数模型，并生成
模型可见的 JSON Schema。

不允许：

- `*args` 和 `**kwargs`；
- positional-only 参数；
- 没有类型标注的参数；
- 名称不是 `context` 的 `ToolContext` 参数。

同步函数通过 `asyncio.to_thread()` 执行；异步函数在事件循环中 await。线程中的同步函数
在外层超时后通常不能被强制终止，所以外部 I/O 优先使用支持超时和取消的异步客户端。

### 7.3 ContextualTool 和 ToolContext

`ContextualTool` 是独立的可选能力 Protocol。Runtime 已经从 ToolRegistry 得到一个 Tool 后，
再用它判断是否支持：

```python
invoke_with_context(arguments, context)
```

`ToolContext` 不进入模型参数 Schema，包含：

- `run_id`
- `resources`
- `artifacts`
- `memories`

它用于把可信的执行期能力注入 Tool，而不是让模型构造数据库连接、文件存储或实时客户端。

### 7.4 Tool 设计规则

- 一个 Tool 做一个原子动作，不承载完整业务工作流；
- 参数和结果使用明确、可验证的结构；
- 读与写使用窄权限，例如 `orders:read`、`orders:refund`；
- 对分页、行数、文件大小和输出大小设置上限；
- 大结果写入 Artifact，只把摘要、样例和引用返回模型；
- Tool 自身仍要执行真实后端鉴权，框架权限标签不能替代租户和数据权限；
- Tool 描述、参数、结果、事件和 Artifact metadata 中不能放密钥。

## 8. Skill 架构

Skill 是一个版本化过程包，不是 Agent 子类，也不是任意 Prompt 文本。

一个 Skill Manifest 声明：

- 名称、版本和描述；
- `allowed-tools`
- `required-tools`
- `required-permissions`
- 正文中的流程说明。

选择 Skill 时，Runtime 会校验：

```text
Skill 在 Profile.skills 中允许
required-tools 已注册且被 Profile 启用
required-permissions 已被 Profile 授予
required-tools 是 allowed-tools 的子集
```

当选择了 Skill，本次 enabled Tool 是 Profile Tool 与所选 Skill allowlist 的交集。
Skill 指令会进入 system instructions，所选名称和版本会进入 Run 与事件。

当前 Skill 选择是显式的：

```python
await agent.run("...", skills=("order-analysis",))
```

基于语义的自动 Skill 选择尚未实现。

## 9. Supervisor 架构

Supervisor 在安全边界调用：

- `before_model(context)`
- `before_tool(context, call)`
- `after_tool(context, call, result)`

它返回：

```text
CONTINUE    正常继续
REDIRECT    注入系统消息，要求模型改变策略
STOP        转入 FAILED / CANCELLED / LIMIT_REACHED
```

默认组合包含：

| 策略 | 行为 |
| --- | --- |
| `ExecutionBudget` | 限制模型 step 和 Tool 调用总数。 |
| `DuplicateToolCallDetector` | 检测连续相同 Tool 参数，要求改变策略。 |
| `NoProgressDetector` | 检测连续 Tool 失败，要求重新规划。 |

Supervisor 适合运行保护和策略干预，不适合放业务工作流。业务审批可以使用
`WaitForInput` 或显式业务 Tool。

## 10. Run 生命周期

状态机允许的主要转换：

```text
CREATED ──► RUNNING ──► COMPLETED
                  ├──► FAILED
                  ├──► CANCELLED
                  ├──► INTERRUPTED
                  ├──► LIMIT_REACHED
                  └──► WAITING ──► RUNNING
                                ├► CANCELLED
                                ├► INTERRUPTED
                                └► FAILED
```

`INTERRUPTED` 表示底层 asyncio 执行任务被中断，而不是用户通过 `Agent.cancel()` 发起的
业务取消；当前版本将其作为不可恢复终态。所有终态均不能再次转换。

### 10.1 同步等待与后台运行

```python
result = await agent.run("...")
```

等待当前执行段结束。

```python
handle = await agent.start("...")
```

创建当前事件循环中的 `asyncio.Task` 并返回 `RunHandle`。Handle 支持：

- `result()`
- `cancel()`
- `get_run()`
- `events()`
- `stream(after_sequence=...)`

RunHandle 不是跨进程任务句柄；进程重启后需要外部调度系统恢复工作。

### 10.2 Event 和游标

每个 Event 在一个 Run 内有递增 `sequence`。消费者保存最后的 sequence 后可以：

```python
handle.stream(after_sequence=last_sequence)
```

从持久化历史补齐，再跟随新事件。Redis 只负责通知加速，持久化 EventStore 才是事实来源。

### 10.3 WAITING 与恢复

Tool 返回：

```python
WaitForInput(prompt="请确认是否继续")
```

Runtime 会：

1. 保存 PendingInput；
2. 将状态切到 WAITING；
3. 保存 RuntimeCheckpoint；
4. 释放当前执行段的 Resource；
5. 返回 WAITING AgentResult。

恢复：

```python
await agent.resume(run_id, answer)
```

Checkpoint 的 `claim()` 必须是原子的，保证并发恢复只有一个成功。用户回答会成为原 ToolCall
对应的 Tool 消息，然后继续模型循环。

## 11. Resource 架构

Resource 用于需要生命周期管理的状态对象，例如：

- 数据库事务或会话；
- BrowserSession；
- SandboxSession；
- MCP ClientSession；
- 临时工作区；
- 业务 API 会话。

`ResourceSpec` 包含名称、异步上下文管理器工厂和 `eager` 标记。默认懒获取，同一执行段内
只获取一次，结束时按获取顺序逆序释放。

边界规则：

- Resource 在同一异步任务中获取和释放；
- WAITING 会释放 Resource，resume 会重新获取；
- Checkpoint 只保存可序列化 ID，不保存活连接；
- 需要跨等待延续的外部会话由应用持久化 session ID 并在工厂中重连；
- 获取、释放和取消失败会进入结构化事件或 Result metadata。

## 12. Attachment 与 Artifact

二者都是不可变引用：

```text
Attachment   Run 开始前已有的输入
Artifact     Run 执行过程中产生的输出
```

引用包含 ID、名称、媒体类型、大小、校验和、时间和安全 metadata。二进制内容位于
ArtifactStore，不进入模型消息、Event、Run、Result 或 Checkpoint。

Tool 通过 `ToolContext.artifacts`：

- 读取当前 Run 声明的 Attachment；
- 读取当前 Run 自己产生的 Artifact；
- 创建新的 Artifact。

大数据应走 Artifact 数据面：

```text
完整数据 ──► ArtifactStore
摘要/样例/ID ──► ToolResult ──► 模型上下文
```

生产环境的下载授权、病毒扫描、租户归属、对象存储和保留策略属于应用层。

## 13. Memory

Memory 是可选检索端口，不是自动拼接到 system prompt 的字符串。

核心契约：

- `MemoryRecord`
- `MemoryQuery`
- `MemoryMatch`
- `MemoryRetriever`
- `MemoryManager`

Runtime 可以在第一次模型调用前执行一次有界检索。默认失败策略是 `BEST_EFFORT`，也可以
选择 `REQUIRED`。

注意：当前 `OpenAIChatProvider` 不会映射结构化 Memory，检测到它会明确抛出
`UnsupportedMemoryError`。要使用自动 Memory，需要实现能够安全映射 MemoryMatch 的
Provider，或通过 Tool 主动检索、筛选和总结。

向量化、索引、写回、删除、保留、租户授权和数据加密不属于核心 Runtime。

## 14. Provider

所有模型适配器实现：

```python
class ModelProvider(Protocol):
    @property
    def name(self) -> str: ...

    async def complete(self, request: ModelRequest) -> ModelResponse: ...
```

Runtime 只认识 Provider 中立模型：

```text
ModelRequest
  messages / tools / tool_choice / model / metadata / attachments / memories

ModelResponse
  content / tool_calls / finish_reason / usage / provider_metadata
```

Provider 负责：

- 把核心 Message 和 ToolDefinition 映射成厂商格式；
- 调用厂商 SDK；
- 把响应规范化为 ModelResponse；
- 对不支持的核心字段明确拒绝，不能静默丢弃。

当前 OpenAIChatProvider：

- 使用异步 Chat Completions；
- 支持消息、函数 ToolCall、ToolChoice、Usage 和部分元数据；
- 非流式；
- 不转发任意 `ModelRequest.metadata`；
- 不映射 Attachment；
- 不映射 Memory；
- 不负责读取 ArtifactStore。

未来 Responses API 应实现为独立 Provider，而不是继续向 Chat Completions 适配器堆条件。

## 15. Store 与可观察性

核心定义四类主要存储端口：

| Protocol | 责任 |
| --- | --- |
| `RunStore` | 创建、读取、保存 Run 和协作式取消标记。 |
| `EventStore` | 写入并列出有序 Event。 |
| `EventStream` | 可选的游标订阅能力。 |
| `CheckpointStore` | WAITING 状态保存、加载、原子 claim 和删除。 |
| `ArtifactStore` | Attachment/Artifact 引用与二进制内容。 |

本地和测试默认使用内存实现。

PostgreSQL 适配器可以同时实现 Run、Event、Checkpoint 和 Artifact 端口。RedisEventStore
包装一个持久化 EventStore：先持久化事件，再发布 sequence 通知；订阅者始终通过游标从
持久化 Store 对账。

Redis 不是 Run 历史数据库，也不是任务队列。

## 16. 可选能力

### 16.1 FastAPI Server

`base_agent.server` 将 Agent API 映射为：

- 创建和查询 Run；
- 取消与恢复；
- Event 历史和 SSE；
- Artifact 引用与可选内容下载。

它不提供：

- 身份认证和多租户授权；
- 上传安全策略；
- 跨进程任务耐久性；
- 分布式 lease、路由和调度。

### 16.2 MCP

MCP 在这里是 Tool transport，不是第二套 Agent Runtime。适配器：

- 分页发现远程 Tool；
- 可加名称前缀防冲突；
- 本地校验 JSON Schema、权限和超时；
- 将 MCP 错误规范化为 ToolResult；
- 过滤协议私有 metadata。

当前只支持 MCP Tool。Resources、prompts、sampling、roots 和 elicitation 不会自动注入核心。

### 16.3 Sandbox

SandboxSession 提供：

- argv 执行；
- 有界文本读取；
- 有界文本写入。

Docker 实现默认采用一次执行段一个可销毁容器，并限制网络、root filesystem、capability、
PID、CPU、内存、输出和文件。Docker 共享宿主内核，仍需要部署侧的镜像审查和宿主加固。

### 16.4 Browser

BrowserSession 提供导航、快照、选择器交互和截图。Playwright 实现：

- 通过 Resource 管理 BrowserContext 生命周期；
- 对 URL 和请求实施网络策略；
- 默认拒绝私网和非全局地址；
- 将截图写成 Artifact，不把 base64 放入 ToolResult；
- 不开放任意 JavaScript、下载、上传、扩展或宿主浏览器 Profile。

### 16.5 Flow 定义与调用边界

当前已经实现简单多 Agent Flow 的定义层、持久化生命周期内核和第一个可执行顺序策略：

- `FlowDefinition`：版本化 Flow ID、命名 Agent 绑定、Strategy 名称和调用上限；
- `FlowAgent`：稳定 `agent_key` 到 `AgentDefinition` 的绑定；
- `AgentHandoff`：Agent 间显式传递的摘要、JSON 数据和 Artifact 引用；
- `AgentInvocationRequest/Result`：带 flow_run_id、invocation_id、sequence 和 agent_key 的
  调用信封；
- `FlowRunState/AgentInvocation`：不可变、可序列化的顶层 Flow 与子调用生命周期；
- `FlowLifecycle`：统一推进 Flow 状态，并将新快照与事件作为一个原子提交；
- `FlowRepository`：以 revision/CAS 防止并发覆盖的持久化端口；
- `InMemoryFlowRepository`：为测试和单进程应用提供连续事件序列与防御性副本；
- `PostgresFlowRepository`：在独立 Flow 表中持久化快照与连续事件，并把一次状态转换
  及其全部事件放入同一个数据库事务；
- `FlowExecutionLease/FlowLeaseRepository`：为 worker 提供单所有者认领、续租、释放、
  单调 attempt 和随机 fencing token；
- `FlowExecutionRunner`：在队列无关的执行边界中统一 claim、自动 heartbeat、丢失所有权
  后取消本地 handler，以及 finally release；
- `FlowWorkSource/FlowWorkCommand`：提供幂等投递、延迟 retry、超时重投，以及独立于
  execution lease 的 delivery fencing；
- `AgentInvoker`：FlowStrategy 调用或恢复 Agent 的 Protocol；
- `FlowInput/FlowResult`：面向应用的顶层输入和聚合结果；
- `SequentialFlowStrategy`：按声明顺序各调用一个命名 Agent，并显式构造 Handoff；
- `FlowBudget/FlowBudgetPolicy`：统一约束 Invocation、Token、模型调用、Tool 调用和墙钟时间；
- `AgentRuntimeInvoker`：将命名 Agent 映射到已配置的现有 Agent Runtime；
- `CancellableAgentInvoker`：把已提交的 Flow 终态传播到活动子执行；
- `DefaultAgentInvocationPromptBuilder`：仅把原始任务与受限 Handoff 构造成模型上下文；
- `ScriptedAgentInvoker`：不执行真实 Agent 的确定性 Flow 测试替身。

Handoff 不包含上一个 Agent 的完整 Message 历史，不继承其 Tool 和权限，也不携带活的
Resource。详细契约见 [`FLOWS.md`](FLOWS.md)。

`FlowRunState` 已经约束单一活动 Invocation、连续 sequence、Definition fingerprint、
WAITING/resume、Usage 聚合和终态传播。每次转换递增 revision；`FlowLifecycle` 将替换
快照与 Flow/AgentInvocation Event 原子写入，WAITING 和父子终止事件也不会出现半提交。
`SequentialFlowStrategy` 现在能够自动完成顺序推进、Usage 聚合、失败短路、调用上限、
WAITING/resume 和取消传播。resume 会先原子认领 WAITING 状态并写入 resumed 事件，再调用
外部 transport，避免两个协调者同时执行恢复副作用。

`AgentRuntimeInvoker` 要求每个 Agent 的 Definition fingerprint 与 Flow 绑定完全一致。
不同 Agent 可以保留各自的 Run、Event、Checkpoint 和 Artifact Store，普通开发者因此能
直接组合独立构造的 Agent；需要统一持久化边界的生产应用仍可在 composition root 注入共享
Store。子执行以 `invocation_id` 作为内部 Runtime Run ID，同时在 Run metadata 和
`run.created` 事件中记录 `flow_run_id`、`agent_key` 与 Definition identity；因此它不是
游离根 Run。不同 Agent 仍保留各自的 Prompt、Tool、Skill、权限、模型和 Resource 配置。
默认上下文构造器不会合并上一 Agent 的 Message 历史，只传递显式 Handoff，并限制序列化
大小。

`FlowBudget` 属于 FlowDefinition fingerprint 和持久化 FlowRunState，而不是某一个
Agent。Harness 在每次 invoke/resume 前后基于已提交的 InvocationResult 计算总
Invocation、输入/输出/总 Token、模型调用、Tool 调用和经过时间。恰好用满预算时允许当前
Flow 完成，但不能再发起 transport；结果一旦超过预算则立即进入 `LIMIT_REACHED`。
墙钟预算直接包围 Agent transport，超时会取消正在运行的调用。终态事件只记录结构化的
预算 kind、limit 和 actual，不复制 Prompt 或输出。各 Agent 自己的 max_steps 和
max_tool_calls 仍然独立生效，形成“Agent 子边界 + Flow 总边界”。

外部取消通过 `SequentialFlowStrategy.cancel()` 进入统一路径：Harness 先原子提交 Flow
与活动 AgentInvocation 的 CANCELLED 终态，再通过 `CancellableAgentInvoker` 通知子
Runtime。WAITING 子 Run 会删除 Checkpoint；RUNNING 子 Run 接受已有的协作式取消信号；
随后晚到的子结果只能读取父终态，不能覆盖它。重复取消已取消 Flow 是幂等操作。如果第三方
Invoker 不支持取消或传播失败，Flow 仍保持 CANCELLED，并追加
`agent_invocation.cancellation_propagation_failed`；事件只保存关联 ID、Flow 状态和错误
类型，不保存异常正文。

Flow Repository 已提供内存与 PostgreSQL 实现。PostgreSQL 适配器通过 Flow 行锁分配连续
事件 sequence，以 revision/CAS 拒绝陈旧协调者，并在同一事务提交快照与该转换的全部事件。
内存与 PostgreSQL Repository 均已支持执行 lease：同一 Flow 只能有一个未过期 owner；
接管会增加 attempt 并更换 fencing token。旧 worker 即使恢复运行，也无法再提交状态或
追加事件。Flow 一旦进入 lease 模式，后续写入不能退化成无 token 写入。

`FlowExecutionRunner` 已在这些原语上提供自动 heartbeat：续租丢失时立即取消本地 handler，
外层取消或 handler 异常时也会执行 release。Repository fencing 仍是最终保护，因此即使
下游代码延迟响应 asyncio cancellation，旧 token 也不能写入。

Runner 本身不轮询 work source，也不负责 Definition 解析、恢复决策和外部副作用账本；
远程异步 Invoker 后续还需要取消 acknowledgement 或自己的 lease 语义。

现在已提供 `InMemoryFlowWorkSource` 与 `PostgresFlowWorkSource`。PostgreSQL 实现使用
idempotency key 去重，并通过 `FOR UPDATE SKIP LOCKED` 让多个 worker 领取不同的可执行行。
每次 delivery 都有自己的 attempt 和 token：超时后新 delivery 会更换 token，旧 worker
不能 complete 或 retry 新任务。长任务通过 `renew()` 延长 delivery，但不改变 attempt
和 token。delivery token 只治理队列任务结算；真正修改 Flow 时仍必须持有
`FlowExecutionLease`，两层 fencing 不能合并。

WorkCommand 的 `data` 是持久业务数据而非 Event。若其中保存恢复输入或其他敏感内容，宿主
应用必须为它提供与 Run 数据一致的加密、授权、保留和删除策略。

`FlowPollingWorker` 已把 work source 与 execution runner 组合为单并发 durable worker：
每个任务同时维护 delivery heartbeat 和 Flow execution heartbeat；任一所有权丢失都会取消
本地 handler。成功时 complete，handler 失败时按配置延迟 retry，worker task 被取消时会
先尝试把仍持有的任务重新排队。`run_once()` 用于确定性测试和外部循环，`run_forever()`
提供轮询，`stop()` 在当前任务结算后优雅停止。

通用 Worker 不猜测中断副作用是否可重做。应用 handler 仍需根据 WorkCommand、持久 Flow
状态和固定 Definition 做恢复决策。默认日志和 WorkItem 只记录异常类型，不复制异常正文，
避免异常消息携带 Prompt 或用户数据。

现在这层决策也有了明确契约：

- `FlowDefinitionResolver` 按 Run 中固定的 ID 和 version 解析不可变 Definition；
- `DefinitionResolvingFlowWorkHandler` 在 dispatcher 前校验 fingerprint；
- `FlowRecoveryPolicy` 只对白名单安全边界返回 START、ADVANCE、RESUME、FINALIZE 或
  CANCEL；
- 已经终态的 Run 返回 NOOP，保证重复投递不会创建第二个终态；
- 活动 RUNNING Invocation 一律返回 MANUAL_REVIEW；副作用账本证据可供 Operator 判断，
  但默认策略不自动重放一个结果尚未提交的 Agent 调用；
- 缺少恢复输入、Definition 缺失或 fingerprint 改变，统一返回 MANUAL_REVIEW。

MANUAL_REVIEW 会通过 metadata-only `FlowWorkBlockedError` 把任务结算为 `BLOCKED`，只保存
有限 reason code，不保存异常正文。BLOCKED 任务不会再次被普通 worker 领取，避免 poison
job 无限 retry。

`FlowWorkReviewStore` 现在提供独立的可信 Operator 端口，与普通 `FlowWorkSource` 权限面
分离。Operator 可以查看 BLOCKED、批准延迟重试、拒绝归档为 DISCARDED，以及读取不可变
review 历史。审批记录包含 reviewer ID、有限 reason code、decision、delay、idempotency
key 和时间戳；审批记录与 WorkItem 状态在一个事务提交。重复请求不会重复应用，两个并发
冲突决策只有一个能够把任务移出 BLOCKED。

这里的 reviewer ID 只是审计字段，不代表已经授权。核心库不直接暴露无认证 HTTP 管理
接口；宿主应用必须先完成操作员身份、租户/Run ownership 和决策权限校验，再调用 Review
Store。reason code 不允许承载 Prompt、恢复输入或异常正文。

`FlowSideEffectLedger` 已提供 metadata-only 的副作用证据契约，以及内存和 PostgreSQL
实现。每条记录只保存 Flow Run ID、Invocation ID、稳定 operation key/name、retry mode、
下游幂等键的 SHA-256 摘要、PREPARED/STARTED/CONFIRMED/ABORTED 阶段、revision 和时间戳；
不保存原始幂等键、Tool 参数、结果、Prompt 或异常正文。Recovery Worker 只依赖更窄的
`FlowSideEffectEvidenceReader`，不能通过恢复端口改写证据。

状态转换刻意保守：PREPARED 可以进入 STARTED 或 ABORTED；ABORTED 可以在后续已知安全的
尝试中重新进入 STARTED；STARTED 只能进入 CONFIRMED。
STARTED 之后发生超时或进程退出时继续保留 STARTED，不能武断写成“失败”。PREPARED/ABORTED
代表已知未执行；STARTED/CONFIRMED 的 retry mode 与幂等键摘要可辅助人工判断。默认恢复
策略仍会阻止所有活动 Invocation 自动重放。没有账本记录视为“缺少证据”，而不是“没有副作用”。

Tool 侧现在通过 `ToolSideEffectMode` 显式声明 UNSPECIFIED、READ_ONLY、UNSAFE 或
IDEMPOTENT。该元数据属于 Harness 治理面，不进入模型可见的 Tool schema。为了兼容现有
Tool，默认值是 UNSPECIFIED；它不等同于 READ_ONLY，也不产生安全性声明。

`FlowToolSideEffectRecorder` 已把 `ToolExecutor` 自动接到账本：参数和权限校验先于
PREPARED/STARTED；正常返回后写 CONFIRMED；超时、异常、取消或进程退出保留 STARTED。
IDEMPOTENT Tool 必须接收 `ToolContext`，并把其中稳定的 `idempotency_key` 传给真正提供
幂等保证的下游系统，账本只保存 key 摘要。UNSAFE Tool 的相同 operation 一旦已经
STARTED/CONFIRMED，会在再次进入业务函数前被拒绝。

重要副作用还可以声明 `ToolConfirmationMode.REQUIRED`。首次调用在进入账本和业务函数前
返回 WAITING，并持久化绑定 Run、ToolCall、Tool 名称和参数摘要的有限确认请求。确认不复用
自由文本 `resume()`：宿主必须构造带 request ID、APPROVE/REJECT、subject ID 和 reason
code 的 `ToolConfirmation`，通过 `Agent.confirm()` 或
`SequentialFlowStrategy.confirm()` 提交。批准后恢复原始 ToolCall，不重复计数；拒绝不启动
Tool 或账本，而是向模型追加结构化 DENIED 观察。错误 request ID 不消费 Checkpoint。

确认请求和决定会产生 metadata-only Event，不复制 Tool 参数。subject ID 只是审计字段，
核心库不把它当作认证；宿主仍须校验操作员身份、租户/Run ownership 和具体副作用权限。

账本是恢复判断的证据，不是自动重放许可。应用必须把同一个 recorder 注入 Flow-owned
Agent，并把同一 ledger 的只读证据端口注入恢复 handler。如果应用将来实现自己的主动
Invocation 恢复策略，必须恢复原 Invocation 中持久化的原 ToolCall；重新询问模型产生的新
ToolCall ID 是一个新的 operation，不能复用旧幂等键。

## 17. 已支持能力总览

| 能力 | 状态 | 入口 |
| --- | --- | --- |
| 离线确定性 Agent | 已实现 | `FakeModel`、starter `OfflineModel` |
| 完整 Agent 场景测试 | 已实现 | `AgentTestHarness`、`AgentTestRun` |
| 完整 Flow 场景测试 | 已实现 | `FlowTestHarness`、`FlowTestRun` |
| Model → Tool → Model | 已实现 | `ModelToolStrategy` |
| 同步/异步 Tool | 已实现 | `@tool`、`FunctionTool` |
| Tool 参数 Schema 与校验 | 已实现 | Pydantic 动态模型 |
| Tool allowlist、权限和超时 | 已实现 | `ToolExecutor` |
| ToolContext 注入 | 已实现 | `ContextualTool` |
| Tool 副作用分类、确认与 Flow 账本埋点 | 已实现（显式启用） | `ToolSideEffectMode`、`ToolConfirmationMode`、`FlowToolSideEffectRecorder` |
| Tool 大结果通用上限 | 已实现 | `BoundedToolResultPolicy`、`max_tool_result_bytes`、UTF-8 测量 |
| Skill Manifest 和显式选择 | 已实现 | `SkillRegistry` |
| 语义 Skill 自动选择 | 尚未实现 | 应作为独立选择策略 |
| Run/Event/后台执行 | 已实现 | `RunHandle` |
| 人工输入 WAITING/resume | 已实现 | `WaitForInput`、Checkpoint |
| 协作式取消 | 已实现 | RunStore cancel flag |
| 执行预算和无进展检测 | 已实现 | Supervisor |
| 可替换编排策略和 Plan | 已实现 | `OrchestrationStrategy` |
| 多 Agent Flow 定义与调用契约 | 已实现 | `FlowDefinition`、Handoff、`AgentInvoker` |
| 简单顺序 Flow 门面 | 已实现（进程内） | `Flow.sequence(...)`、`FlowRun` |
| Flow 生命周期状态契约 | 已实现 | `FlowRunState`、`AgentInvocation` |
| Flow 原子状态与事件仓储 | 已实现（内存与 PostgreSQL） | `FlowLifecycle`、revision/CAS、`InMemoryFlowRepository`、`PostgresFlowRepository` |
| 顺序多 Agent Flow | 已实现（内存/测试 Invoker） | `SequentialFlowStrategy`、显式 Handoff、WAITING/resume |
| Agent Runtime 调用适配器 | 已实现（进程内） | `AgentRuntimeInvoker`、Definition pinning、关联子 Run |
| Flow 统一预算 | 已实现 | Invocation、Token、模型/Tool 调用、wall-clock timeout |
| Flow 主动取消传播 | 已实现（进程内） | WAITING 清理、RUNNING 信号、晚到结果保护、失败事件 |
| Flow worker ownership | 已实现（底层原语） | claim、heartbeat、release、attempt、fencing token |
| Flow execution runner | 已实现（队列无关） | 自动 claim/heartbeat、丢失 lease 后取消、finally release |
| Flow durable work source | 已实现（内存与 PostgreSQL） | 幂等 enqueue、`SKIP LOCKED` claim、delivery fencing、延迟 retry |
| Flow polling worker | 已实现（单并发） | 双 heartbeat、handler、complete/retry、取消重排、优雅停止 |
| Flow Definition resolution | 已实现 | 精确 ID/version 解析、fingerprint pinning |
| Flow recovery policy | 已实现（保守默认） | 安全边界白名单、NOOP、MANUAL_REVIEW、BLOCKED |
| Flow operator review | 已实现（核心端口） | BLOCKED 查询、批准重试、拒绝归档、幂等审计 |
| Flow 副作用恢复证据 | 已实现（核心端口） | metadata-only 账本、CAS 状态机、内存/PostgreSQL、人工恢复判断证据 |
| 耐久 Flow Runtime | 部分实现 | 原子仓储、worker、恢复、账本、Tool 埋点与人工闭环已完成；仍需 Checkpoint 恢复策略 |
| 执行期 Resource | 已实现 | `ResourceSpec`、`ResourceManager` |
| Attachment/Artifact | 已实现 | `ArtifactStore` |
| Memory 检索端口 | 已实现 | `MemoryRetriever` |
| OpenAI Chat Completions | 已实现、能力有限 | `OpenAIChatProvider` |
| 流式模型输出 | 尚未实现 | 需要新 Provider/Runtime 契约 |
| FastAPI/SSE | 可选实现 | `base_agent.server` |
| PostgreSQL 持久化 | 可选实现 | `base_agent.stores.postgres` |
| Redis 事件通知 | 可选实现 | `base_agent.stores.redis` |
| MCP Tool | 可选实现 | `base_agent.mcp` |
| Docker Sandbox | 可选实现 | `base_agent.sandbox.docker` |
| Playwright Browser | 可选实现 | `base_agent.browser.playwright` |
| CodingBundle | 已实现、显式启用 | `docker_coding_bundle` |
| Web Search | 已实现、显式启用 | `web_search_bundle`、`BraveWebSearchProvider` |
| 只读 DataSource | 已实现、显式启用 | `data_source_bundle`、`MtbiCliDataSource` |

## 18. 如何增加能力

### 18.1 增加一个业务 Tool

优先放在应用项目，而不是核心包：

1. 在应用的 `tools.py` 定义有类型的原子函数；
2. 声明窄权限、超时和有界返回；
3. 将实现加入应用 `TOOLS`；
4. 从明确的 enabled 集合推导 Profile Tool 名称；
5. 显式授予 Profile permissions；
6. 用 ToolHarness 测试成功、无权限、参数错误、超时和大结果边界；
7. 用 FakeModel 构造应用 Agent，再通过 AgentTestHarness 验证完整模型工具循环、Run 和
   Event。

### 18.2 增加一个 Skill

1. 创建 `skills/<name>/SKILL.md`；
2. 声明版本、allowlist、required Tool 和 permissions；
3. 在 Profile 中允许 Skill；
4. 用 SkillHarness 检查 Manifest 和 Profile；
5. 用 AgentTestHarness 验证 Skill 指令、Tool 边界、Run 事件和最终结果。

业务流程优先放 Skill，原子动作放 Tool。

### 18.3 增加一个 Provider

1. 实现 `ModelProvider`；
2. 明确映射每种 MessageRole、ToolDefinition 和 ToolCall；
3. 把厂商错误转换成清晰的 Provider 边界错误；
4. 规范化 Usage 和 provider metadata；
5. 对 Attachment、Memory 和 metadata 明确支持或拒绝；
6. 使用内存 fake client 测试，不依赖真实 API Key；
7. 第三方 SDK 放入新的 optional extra，避免污染基础安装。

### 18.4 增加一个有状态能力

例如数据库会话、搜索客户端或远程浏览器：

1. 先定义最小 Protocol；
2. 用 `ResourceSpec` 管理获取和释放；
3. 通过 ToolContext 提供给 Tool；
4. 为本地测试提供 fake/in-memory 实现；
5. 明确 WAITING 后是重连还是新会话；
6. 不把活对象放入 Context 快照或 Checkpoint。

### 18.5 增加 Supervisor

1. 实现 before_model、before_tool 或 after_tool；
2. 将状态保存在 `context.supervision_data`；
3. 返回结构化 CONTINUE/REDIRECT/STOP；
4. 测试边界值、WAITING/resume 和终态；
5. 不在 Supervisor 内执行不可审计的业务副作用。

### 18.6 增加编排策略

1. 实现 `OrchestrationStrategy.advance(context, services)`；
2. 每次 advance 必须有界，不能隐藏无限循环；
3. 通过 RuntimeServices 使用 Provider、Tool、Store、Supervisor 和 Manager；
4. 通过状态机进入合法状态；
5. Plan 更新使用公共的 `update_execution_plan()`；
6. 保持 Run/Event/Checkpoint 生命周期由 AgentRuntime 管理；
7. 测试取消、预算、失败、WAITING 和恢复。

### 18.7 增加 Store

实现对应 Protocol，并特别保证：

- Run 创建与更新的一致性；
- 每个 Run 的 Event sequence 连续且并发安全；
- Checkpoint claim 原子；
- EventStream 按游标无重复补齐；
- Artifact 内容与引用一致；
- 多租户访问控制由应用层明确实施。

### 18.8 增加新的核心横切能力

对于 ToolResult 上限、模型流式输出、上下文压缩等横切能力，按以下顺序设计：

```text
不可变核心模型
    ↓
Protocol / 配置契约
    ↓
内存或 fake 实现
    ↓
Runtime/Strategy 注入点
    ↓
事件和持久化语义
    ↓
可选外部适配器
    ↓
确定性测试、文档和公共导出
```

不要先在 OpenAI、FastAPI 或某个业务 Tool 中实现一套只对单一路径有效的特殊逻辑。

## 19. 新能力的完成标准

每项能力至少回答：

1. 它属于 Tool、Skill、Strategy、Supervisor、Resource、Store 还是 Provider？
2. 核心契约是什么，厂商或业务细节是否泄漏进核心？
3. 输入、输出、权限、大小、超时和取消边界是什么？
4. WAITING/resume 后状态是否可恢复？
5. 哪些内容进入消息、Event、Run、Checkpoint 和 Artifact？
6. 是否存在租户、密钥或敏感内容泄漏？
7. 是否提供无网络的 deterministic test？
8. 是否通过 pytest、Ruff、strict mypy 和 build？
9. 是否更新专题文档、README 入口和公共 `__all__`？
10. 可选依赖是否仍保持可选？

仓库质量门：

```bash
uv run pytest
uv run ruff check .
uv run mypy src
uv build
```

## 20. 推荐学习路线

不要从所有文件同时开始。按下面顺序阅读和运行：

### 阶段一：最小 Agent

1. 运行 [`examples/hello_agent.py`](../examples/hello_agent.py)；
2. 阅读 `AgentDefinition`、`Agent`、`FakeModel`；
3. 理解 Provider 中立的 request/response。

### 阶段二：Tool 循环

1. 运行 [`examples/tool_agent.py`](../examples/tool_agent.py)；
2. 阅读 `tools/decorator.py`；
3. 阅读 `tools/executor.py`；
4. 阅读 `orchestration/model_tool.py`；
5. 对照 [`tests/test_tools.py`](../tests/test_tools.py)。

### 阶段三：Skill

1. 运行 [`examples/skill_agent/run.py`](../examples/skill_agent/run.py)；
2. 阅读 Skill Manifest；
3. 阅读 loader、registry 和 validator；
4. 对照 [`tests/test_skills.py`](../tests/test_skills.py)。

### 阶段四：生命周期

1. 阅读 runtime/context、state_machine 和 engine；
2. 阅读 Run、Event、Checkpoint；
3. 对照 `test_runtime.py`、`test_runs.py`、`test_resume.py` 和 `test_run_handle.py`。

### 阶段五：安全和基础设施

1. 阅读 Supervisor；
2. 阅读 Resource 和 ToolContext；
3. 阅读 Artifact 和 Memory；
4. 最后按需要阅读 PostgreSQL、Redis、MCP、Sandbox、Browser 和 Server。

### 阶段六：应用组合

阅读 [`starter/src/agent_app/agent.py`](../starter/src/agent_app/agent.py)，理解配置、Provider、
Tool、Skill、CLI 和 Server 如何在应用层组合，而不是塞进 Runtime。

## 21. 当前最值得补充的能力

下面是与现有架构一致、且边界已经比较清楚的后续方向：

1. 定义 Harness 配置审计信封，逐步形成 `HarnessSpec`、`HarnessSnapshot` 或等价契约；
2. 增加独立 Verification 契约，覆盖 preflight、outcome、trajectory 和 evaluator；
3. 完成 Event payload 安全策略、HTTP 租户授权和 Tool 副作用治理；
4. ToolResult 统一大小保护、Artifact 引用信封和有界事件；
5. Coding 的安全项目快照、Patch Artifact 和经批准的 Patch 应用；
6. 带 SSRF/重定向/内容上限策略的已知 URL 获取 Tool；
7. OneSQL 长查询的 detach/fetch/cancel、恢复和扫描成本限制；
8. 上下文预算与历史压缩策略，明确 Event/Checkpoint 保真和模型上下文裁剪的区别；
9. 独立的 Responses API Provider，支持经过授权的 Attachment 映射；
10. Memory-capable Provider 或安全的 Memory 注入策略；
11. 模型输出流式契约及其与 EventStream 的关系；
12. 面向生产的外部任务 runner、lease 和进程重启恢复；
13. 对象存储 ArtifactStore 与租户授权；
14. 显式 Skill 选择器或 Router，但保持选择结果可观察、可验证；
15. 应用层多 Agent Strategy，不把业务角色写死进核心；
16. 在 Coding/Web Search/DataSource 的真实使用边界稳定后，再评估通用 Capability API。

这些能力应按第 18.8 节的顺序扩展，并优先补充测试与边界说明，再接具体厂商实现。
详细任务、待决设计和验收标准统一记录在 [`TODO.md`](TODO.md)。

## 22. 专题文档索引

- 后续任务：[`TODO.md`](TODO.md)
- 总体边界：[`ARCHITECTURE.md`](ARCHITECTURE.md)
- 目标架构设计：[`ARCHITECTURE_DESIGN_ZH.md`](ARCHITECTURE_DESIGN_ZH.md)
- AgentDemo/mock-manus 参考决策：[`REFERENCE_DESIGN.md`](REFERENCE_DESIGN.md)
- Tool：[`TOOLS.md`](TOOLS.md)
- Skill：[`SKILLS.md`](SKILLS.md)
- Run 与 Event：[`RUNS.md`](RUNS.md)
- 编排与 Plan：[`ORCHESTRATION.md`](ORCHESTRATION.md)
- Flow 定义与调用边界：[`FLOWS.md`](FLOWS.md)
- Resource：[`RESOURCES.md`](RESOURCES.md)
- Attachment/Artifact：[`ARTIFACTS.md`](ARTIFACTS.md)
- Memory：[`MEMORY.md`](MEMORY.md)
- Provider：[`PROVIDERS.md`](PROVIDERS.md)
- Server：[`SERVER.md`](SERVER.md)
- PostgreSQL：[`POSTGRES.md`](POSTGRES.md)
- Redis：[`REDIS.md`](REDIS.md)
- MCP：[`MCP.md`](MCP.md)
- Sandbox：[`SANDBOX.md`](SANDBOX.md)
- Browser：[`BROWSER.md`](BROWSER.md)
- Coding：[`CODING.md`](CODING.md)
- Web Search：[`WEB_SEARCH.md`](WEB_SEARCH.md)
- 只读 DataSource：[`DATA_SOURCES.md`](DATA_SOURCES.md)
- 测试：[`TESTING.md`](TESTING.md)
- 故障排查：[`TROUBLESHOOTING.md`](TROUBLESHOOTING.md)
- 可复制 starter：[`starter/README.md`](../starter/README.md)
