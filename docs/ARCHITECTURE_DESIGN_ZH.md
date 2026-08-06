# base-agent 整体架构设计

`base-agent` 的首要目标是：

> 让 Agent 开发者专注编写 Prompt、Tool、Skill 和少量 Agent/Flow 编排策略，通过组合
> 而不是开发 Runtime，就能快速构建一个可运行、可测试、可扩展的单 Agent 或简单
> 多 Agent 应用。

Run、Event、Checkpoint、Worker 和持久化是支撑这一目标的运行时能力，不是
`base-agent` 对使用者呈现的主要复杂度。

## 1. 文档目标

本文定义 `base-agent` 的目标软件架构，用于指导后续功能开发、架构重构、生产化建设和
下游应用集成。

本文不是当前源码的逐文件说明，也不是某一次重构的类设计。当前实现请参考
[中文架构与扩展指南](ARCHITECTURE_GUIDE_ZH.md)。本文重点回答：

- 其他开发者如何只编写 Prompt、Tool、Skill、Flow 和配置就构建 Agent 应用；
- 哪些复杂度由框架吸收，哪些能力由 Agent 应用显式选择；
- Agent 定义、能力扩展和执行内核之间如何分层；
- 系统由哪些逻辑子系统组成，各自承担什么职责；
- Run、Plan、Tool、Checkpoint、Event 和 Resource 如何协作；
- 嵌入式、HTTP 服务和持久化 Worker 三种部署形态如何共享同一内核；
- 一致性、安全性、恢复能力和扩展性应达到什么目标；
- 当前实现与目标架构之间有哪些差距，应按什么顺序演进。

文中使用以下标记：

- **当前**：当前仓库已经具备的能力；
- **目标**：目标架构要求，但可能尚未实现；
- **非目标**：明确不由核心包负责的能力。

## 2. 背景与问题定义

团队开发 Agent 时，真正具有业务差异的部分通常是：

- Agent 的角色、目标和 Prompt；
- 可以调用哪些 Tool；
- Skill 如何描述一个可复用任务流程；
- 多个专业 Agent 如何通过一个简单 Flow 顺序协作或按 Plan 分工；
- 使用哪个模型以及允许哪些权限；
- 少量业务特有的编排或结果处理策略。

开发者不应该为了增加一个查询 Tool 或修改一个业务 Skill，就重新实现模型循环、状态机、
取消、等待恢复、事件、资源释放和持久化。如果每个 Agent 都复制这些代码，就会产生不同
的执行语义、安全策略和故障处理方式。

因此，`base-agent` 首先要解决的是：

> 把所有 Agent 都需要但不应重复开发的运行时能力沉到框架中，让业务开发者通过声明和
> 组合能力构建 Agent。

在这个首要目标之下，框架还必须解决以下通用工程问题：

- 一次执行如何被唯一标识、查询和审计；
- 模型和 Tool 多轮执行时如何限制预算、权限和无进展循环；
- 执行如何暂停、接受人工输入并从同一个上下文继续；
- HTTP 客户端断开、异步 Task 被取消、进程退出时，Run 应处于什么状态；
- 浏览器、沙箱、MCP 客户端等有状态资源如何按执行范围安全释放；
- 不同模型、数据库和基础设施如何替换，而不侵入运行时核心；
- 下游业务如何增加能力，而不复制或修改 Agent 执行内核。

所以，`base-agent` 既不是某个模型 SDK 的简单封装，也不是一个通用任务调度平台。它的
产品定位是：

> 面向 Agent 开发者的单 Agent/Flow 应用开发框架，以及隐藏在开发接口之后的统一执行
> 内核。

## 3. 建设目标和非目标

### 3.1 建设目标

1. **Agent 开发简单**：开发者主要编写 Prompt、Tool、Skill、Flow 和 Agent 配置，不
   编写 Runtime 生命周期代码。
2. **组合优于继承**：通过组合模型、能力包、Skill 和策略形成 Agent，不建立业务 Agent
   深继承树。
3. **简单多 Agent 协作**：通过 Flow 组合命名 Agent，支持固定管道、路由和
   Planner/Executor 分工，而不引入独立多 Agent 平台。
4. **开箱即可测试**：Tool、Skill、Agent 和 Flow 都能使用 FakeModel 和 Harness 离线
   验证。
5. **统一执行语义**：Agent 和 Flow 执行都以 Run 为边界，共享状态、事件、预算和结果。
6. **能力显式授权**：Tool 的注册、启用、权限和副作用策略彼此分离。
7. **基础设施可替换**：模型、存储、Resource、Artifact 和通知系统通过 Port 注入。
8. **执行可观察和控制**：统一处理事件、取消、中断、等待、恢复、预算和资源清理。
9. **本地优先且可生产化**：无外部基础设施即可运行；生产部署可增加 PostgreSQL、
   Redis、HTTP 服务和外部 Worker。
10. **业务逻辑外置**：业务 Prompt、Skill、Tool、Agent 和 Flow 由下游应用拥有。
11. **渐进演进**：目标架构能够通过兼容性重构实现，不要求重写现有代码。

一个简单 Agent 的目标使用方式应接近：

```python
profile = AgentProfile(
    id="order-assistant",
    instructions=load_prompt("prompts/order_assistant.md"),
    model="default",
    tools=("get_order", "search_policy"),
    skills=("handle-refund-question",),
    permissions=frozenset({"orders:read", "policy:read"}),
)

agent = Agent(
    profile=profile,
    model=model,
    tools=(get_order, search_policy),
    skill_registry=skills,
)

result = await agent.run("帮我确认这个订单是否可以退款")
```

开发者不需要了解 RuntimeContext、CheckpointStore、Event sequence 或状态持久化顺序，
除非正在开发框架扩展或生产基础设施 Adapter。

两个专业 Agent 的目标组合方式应接近：

```python
flow = Flow(
    id="mtbi-analysis",
    agents={
        "query": mtbi_agent,
        "analysis": data_analysis_agent,
    },
    strategy=SequentialFlow(
        stages=(
            FlowStage(agent="query", prompt=query_stage_prompt),
            FlowStage(agent="analysis", prompt=analysis_stage_prompt),
        )
    ),
)

result = await flow.run("分析本周付费用户的留存变化")
```

具体类名属于后续 API 设计，关键约束是业务开发者只描述 Agent、阶段和交接数据，不实现
Run 生命周期。

### 3.2 非目标

核心包不直接提供：

- Web UI 或完整 Agent 产品；
- 身份提供商、企业权限中心或多租户管理后台；
- 通用分布式任务队列；
- Kubernetes、Prometheus、Grafana 等具体部署方案；
- 默认开启的宿主机 Shell、浏览器、Docker 或任意网络访问；
- 业务 Agent、BI 修复流程或领域数据模型；
- 自治多 Agent 社会、动态组织、复杂分布式工作流平台；
- 跨任意 Tool 的“恰好一次”副作用保证。

HTTP、PostgreSQL、Redis、MCP、Docker、Playwright 和模型 SDK 均属于可选适配器或宿主
应用能力。

## 4. 架构原则

### 4.1 Agent 开发者体验优先

先优化“定义、运行和测试 Agent”的主路径，再暴露高级运行时扩展。底层能力不能迫使普通
Agent 开发者理解状态机、事务、Worker 或基础设施对象。

### 4.2 Convention with Explicit Boundaries

简单场景提供约定和默认实现，高风险能力保持显式。开发者可以少配置，但框架不能通过
自动注册推导权限或自动开放副作用能力。

### 4.3 Agent 与 Flow 分层

Agent 封装一个角色的 Prompt、Tool、Skill、模型和权限；Flow 只协调命名 Agent 之间的
顺序、路由、Plan 和数据交接。Flow 不复制 Agent 的 Model/Tool 循环。

### 4.4 Library First

核心首先是 Python Library。CLI、HTTP Server 和 Worker 都是宿主形态，不能反向污染
核心执行语义。

### 4.5 Run 是执行一致性边界

一个 Run 关联当前快照、有序事件、Checkpoint、Plan、Attachment、Artifact 和可选
Conversation Turn。Run 状态是对外查询执行结果的权威状态。

### 4.6 生命周期只有一个控制者

状态转换、终态判定、Checkpoint 创建或删除、终态事件和 Conversation Turn 收尾必须
通过统一生命周期边界完成。Strategy 和 Tool 不得各自实现这些规则。

### 4.7 决策与副作用分离

编排策略决定下一步做什么；运行时负责调用模型、执行 Tool 和提交状态。存储实现负责
原子性，而不是决定业务状态。

### 4.8 Port 稳定，Adapter 可选

核心依赖协议和领域模型。OpenAI、PostgreSQL、Redis、MCP、Docker、Playwright、Brave
及企业平台集成都只能依赖核心 Port。

### 4.9 默认最小权限

Tool 已注册不代表已向模型启用，已启用不代表已授权。网络、文件、进程、浏览器和数据
访问必须显式开放。

### 4.10 持久事实先于通知

Run、Event 和 Checkpoint 等事实必须先持久化，再发送 Redis、SSE 或其他通知。通知丢失
可以通过持久事件游标修复。

### 4.11 不序列化活资源

数据库连接、浏览器页面、容器、线程和 MCP Session 不进入 Run、Event 或 Checkpoint。
Checkpoint 只保存可序列化状态和外部资源引用。

### 4.12 失败语义显式

业务取消、异步 Task 中断、模型失败、Tool 失败、预算耗尽、等待输入和 Worker 丢失必须
具有不同语义，不能统一压缩成异常字符串。

## 5. 系统上下文

```text
Agent 开发者
  │
  │ 编写 Prompt / Tool / Skill / Agent / Flow
  ▼
┌──────────────────────────────────────────────┐
│ Agent 应用                                   │
│ 单 Agent 或命名 Agent Flow                   │
│ 业务策略 + base-agent 公共开发 API           │
└──────────────────────┬───────────────────────┘
                       │ run / start / resume
                       ▼
┌──────────────────────────────────────────────┐
│ base-agent 执行内核                          │
│ Agent 调用、Flow 编排、Run、监督、事件       │
└────────┬──────────────┬──────────────┬───────┘
         │              │              │
         ▼              ▼              ▼
   Model Provider   Capability      Persistence
   OpenAI / CLI     MCP/Browser     PostgreSQL
                    Sandbox/Data    Redis/Object

终端用户 / 业务系统
  │
  └── 通过 CLI / Notebook / HTTP / Worker 使用 Agent 应用
```

### 5.1 外部参与者

| 参与者 | 与 base-agent 的关系 |
| --- | --- |
| Agent 开发者 | 编写 Prompt、Tool、Skill、Agent 和 Flow，组合并测试 Agent 应用。 |
| 业务应用 | 承载 Agent，选择 CLI、HTTP 或 Worker 等运行方式。 |
| 终端用户 | 通过宿主应用发起、查询、取消或恢复 Run。 |
| 模型服务 | 实现 `ModelProvider`，接收 Provider 中立请求并返回结构化响应。 |
| Tool 后端 | 承担搜索、数据、浏览器、文件、沙箱或业务操作。 |
| 持久化系统 | 保存 Run、Event、Checkpoint、Conversation 和 Artifact。 |
| 运维系统 | 负责进程、队列、Worker、密钥、网络、备份、监控和告警。 |

### 5.2 信任边界

- 用户输入、模型输出、远程 Tool 结果和外部网页均为不可信数据；
- Tool 实现属于受信代码，但其后端响应仍需要验证；
- HTTP 身份与租户归属由宿主应用提供；
- Worker 可以拥有内部执行权限，但不得自动继承终端用户全部权限；
- Event、日志、Checkpoint 和 Artifact 都可能包含敏感信息，需要独立访问和保留策略。

## 6. 核心业务场景

### 6.1 构建一个简单 Agent

开发者选择模型，编写 System Prompt，使用 `@tool` 声明少量业务 Tool，按需编写
`SKILL.md`，然后通过 AgentProfile 和 `Agent` 完成组合。整个过程不需要：

- 继承 AgentRuntime；
- 修改 Model/Tool 主循环；
- 手工创建 RuntimeContext；
- 处理 Event sequence；
- 编写取消、等待或资源释放逻辑。

### 6.2 测试 Agent 能力

开发者可以分别测试：

- Tool 参数 Schema、权限和结果；
- Skill 的 Manifest、Tool 依赖和指令加载；
- 使用 FakeModel 编排的完整 Agent 行为；
- Run 状态、Event 和 Artifact 等可观察结果。

测试默认不需要真实模型、数据库、浏览器或网络。

### 6.3 复用 Agent 能力

通用 Tool 组成 Toolkit，业务流程组成版本化 Skill，多个 Agent 可以使用不同 Profile
选择相同能力。注册能力不会自动启用或授权能力。

### 6.4 构建简单多 Agent Flow

开发者把多个已经定义好的 Agent 以名称注册到 Flow，再选择：

- 固定顺序管道，例如“查询 Agent → 分析 Agent”；
- 路由 Flow，根据任务类型选择一个 Agent；
- Planner/Executor Flow，由规划 Agent 生成或更新 Plan，再把 Step 分配给执行 Agent。

Flow 负责输入输出交接、Agent 选择和整体完成条件。每个 Agent 仍使用自己的 Prompt、
Tool、Skill、权限和上下文边界。

### 6.5 普通执行

调用方提交 Prompt，Runtime 在预算内执行 Model → Tool → Model 循环，最终返回
`COMPLETED`、`FAILED`、`CANCELLED`、`INTERRUPTED` 或 `LIMIT_REACHED`。

### 6.6 后台执行与观察

调用方启动后台 Run，通过 RunHandle、Event API 或 SSE 查询状态并按 sequence 增量消费
事件。取消观察者不能隐式取消底层 Run。

### 6.7 Conversation Turn

一个 Conversation 按顺序关联多个 Run。任何时刻最多存在一个活动 Turn；只有已经完成
的历史消息进入下一 Turn。

### 6.8 人工等待与恢复

Tool 返回 `WaitForInput` 后，Run 进入 `WAITING`，释放执行期 Resource 并保存
Checkpoint。用户提交输入后，系统原子取得恢复权并继续同一个 Run。

### 6.9 Plan 执行与 Agent 分工

调用方提供 Plan，或显式请求模型生成 Plan。Plan Step 通过同一模型、Tool、权限、预算、
事件和 Run 生命周期执行。Flow 可以为 Step 指定目标 Agent；Plan 自身不拥有 Agent，也
不替代 Flow。

### 6.10 业务取消

`Agent.cancel()` 表示调用方接受的业务取消请求。Runtime 在安全检查点停止后，将 Run
收敛到 `CANCELLED`。

### 6.11 任务中断

执行协程收到非业务取消导致的 `CancelledError` 时，当前实现将 Run 收敛为不可恢复的
`INTERRUPTED`，释放 Resource、删除 Checkpoint，并释放 Conversation Turn。

### 6.12 Worker 丢失与恢复

目标部署中，Worker 通过租约声明执行权。Worker 进程消失后，租约过期，恢复器根据
Checkpoint、Tool 副作用记录和恢复策略决定重新执行、人工确认或标记 `INTERRUPTED`。
该能力当前尚未由通用 Runtime 实现。

## 7. 质量属性

### 7.1 质量属性优先级

| 优先级 | 属性 | 设计目标 |
| --- | --- | --- |
| P0 | 易用性 | 简单 Agent/Flow 只需要 Prompt、Tool、Skill、命名 Agent 和少量配置。 |
| P0 | 可测试性 | Agent 和 Flow 可以离线、确定性测试，不依赖生产基础设施。 |
| P0 | 正确性 | Run 状态、终态事件、Checkpoint 和 Conversation Turn 不相互矛盾。 |
| P0 | 安全性 | 未显式授权的能力不可执行；敏感数据不会默认进入可广泛读取的事件。 |
| P1 | 可扩展性 | 新 Tool、Skill 和 Agent 不修改 Runtime；新 Adapter 只实现稳定 Port。 |
| P1 | 可恢复性 | WAITING 可确定恢复；中断和 Worker 丢失不会永久伪装成 RUNNING。 |
| P1 | 可观察性 | 可通过稳定 ID 关联 Run、Model、Tool、Event、日志、指标和 Trace。 |
| P1 | 兼容性 | 本地无基础设施用法保持简单；可选 Adapter 不进入基础安装。 |
| P2 | 性能 | 不因观察、通知或大结果阻塞核心执行；所有载荷具有上限。 |

### 7.2 关键质量场景

| 场景 | 期望结果 |
| --- | --- |
| 同事增加一个业务查询能力 | 只新增 Tool 和测试，并在 Profile/Skill 中显式启用。 |
| 同事新增一个领域 Agent | 主要新增 Prompt、Profile、Skill 和能力组合，不继承 Runtime。 |
| 同事组合两个专业 Agent | 只定义命名 Agent、Flow 阶段和交接规则，不复制 Agent 循环。 |
| CI 测试 Agent | 使用 FakeModel 和内存实现完成确定性测试，不访问真实模型。 |
| CI 测试 Flow 路由 | 使用脚本化 Agent/FakeModel 验证调用顺序、输入交接和整体结果。 |
| 从本地模式切换到 PostgreSQL | Agent 的 Prompt、Tool 和 Skill 不需要重写。 |
| 并发保存快照和请求取消 | `cancel_requested` 单调保持为真，最终不能错误完成。 |
| 两个请求同时恢复 WAITING Run | 只有一个请求取得恢复权。 |
| Runtime 在 Tool 完成后、保存结果前退出 | 不自动重复非幂等 Tool；进入人工判定或幂等恢复流程。 |
| Redis 不可用 | 事实仍写入持久 EventStore，订阅者通过游标补齐。 |
| HTTP 客户端断开 | 已接受的 Run 或恢复操作不被隐式取消。 |
| Resource 获取一半失败 | 已获取 Resource 按逆序释放，Run 形成明确失败记录。 |
| Event 含敏感 Tool 参数 | 按事件数据策略脱敏或只保存元数据。 |
| 下游增加新 Tool | 不需要继承 AgentRuntime 或修改核心状态机。 |

## 8. 总体逻辑架构

```text
┌──────────────────────────────────────────────────────────┐
│ 1. Agent 应用定义层（由业务开发者编写）                  │
│ Prompt / AgentProfile / Tool / Skill / FlowDefinition    │
├──────────────────────────────────────────────────────────┤
│ 2. Agent 开发 API 与能力 SDK                             │
│ Agent / Flow / @tool / Skill / Toolkit / Testing Harness │
├──────────────────────────────────────────────────────────┤
│ 3. 执行内核（对普通 Agent 开发者隐藏）                  │
│ Execution Loop / Run Lifecycle / Supervision             │
│ AgentInvoker / FlowStrategy / ModelTool / Planning        │
├──────────────────────────────────────────────────────────┤
│ 4. 领域模型与 Port                                       │
│ Run / Event / Plan / Checkpoint / Conversation            │
│ Provider / Repository / Artifact / Memory / Resource      │
├──────────────────────────────────────────────────────────┤
│ 5. 可选 Adapter 与宿主                                   │
│ InMemory / PostgreSQL / Redis / OpenAI / CLI / MCP        │
│ Docker / Playwright / FastAPI / Worker                    │
└──────────────────────────────────────────────────────────┘
```

主路径是“Agent/Flow 应用定义层 → 开发 API → 执行内核”。普通开发者停留在前两层；
只有框架维护者和高级扩展开发者需要理解下面三层。运行时内核只能依赖领域模型与 Port，
Adapter 实现 Port。HTTP、数据库和厂商 SDK 不得成为运行时内核的导入依赖。

## 9. 子系统职责

### 9.1 Agent 定义子系统

这是 `base-agent` 最重要的使用界面，负责表达一个 Agent“是谁、能做什么、受到什么
限制”，以及多个 Agent“如何协作”：

- Prompt：角色、目标、行为边界和输出约定；
- AgentProfile：模型路由、Tool/Skill 名单、权限和预算；
- Tool：原子、类型化的业务能力；
- Skill：可复用的任务知识和 Tool 使用策略；
- Toolkit/Bundle：一组可选择的相关能力；
- FlowDefinition：命名 Agent 集合、Flow 策略、共享输入输出和整体限制；
- 可选 Strategy：普通循环无法表达时才增加的编排策略。

业务 Agent 优先采用数据和组合定义，不创建 `OrderAgentRuntime`、`SqlAgentRuntime`
这样的 Runtime 子类。

### 9.2 开发 API 与测试子系统

负责提供稳定、易用的 Python API：

- 定义 AgentProfile 和静态能力；
- 组合 Provider、Tool、Skill、Store、Resource 和 Supervisor；
- 校验调用参数；
- 暴露运行、后台启动、恢复、取消和查询入口；
- 提供 ToolHarness、SkillHarness、FakeModel 和 Agent 场景测试；
- 提供 FlowHarness 或脚本化 Agent 调用替身；
- 对常见能力提供可复用 Toolkit 和清晰错误信息。

`Agent` 应保持为薄门面。依赖组合由应用的 composition root 完成，不引入重量级 DI
容器。

### 9.3 执行应用子系统

负责组织“启动 Run”“恢复 Run”“取消 Run”等完整用例：

- Conversation Turn 分配；
- Start/Resume 请求的幂等和并发控制；
- 选择本地或持久化执行器；
- 调用 Runtime；
- 对外返回 Result 或 Handle。

该职责可以由一个或多个应用服务承担。是否命名为 `RunService`、`ExecutionService` 或
`RunCoordinator` 属于详细设计决定，不是整体架构的预设。

### 9.4 运行时内核

负责：

- 创建或恢复 RuntimeContext；
- 推进有界执行循环；
- 调用模型和 Tool；
- 进行取消、预算和 Supervisor 检查；
- 管理一次执行段中的 Resource；
- 通过统一生命周期组件提交 Run 变化；
- 构建 AgentResult。

### 9.5 Flow 编排子系统

负责协调多个命名 Agent：

- 保存 Agent key 到 AgentDefinition 的映射；
- 根据固定阶段、路由规则或 Plan Step 选择 Agent；
- 构造明确的 Agent 输入并接收结构化输出；
- 控制共享 Artifact、上下文摘要和阶段结果；
- 聚合 Token、Event、失败和最终输出；
- 将 WAITING、Cancel 和 Interrupt 传播到整个 Flow Run。

Flow 不直接调用另一个 Agent 的公开 `run()` 创建无关联根 Run，而是通过内核
`AgentInvoker` 在当前 Flow 执行范围内调用目标 Agent。

### 9.6 Agent 内编排子系统

负责根据上下文决定下一步动作：

- 普通 Model/Tool 循环；
- ReAct 迭代；
- Plan 生成、执行、复核和总结；
- 将策略私有状态保存为版本化、可序列化数据。

Strategy 不直接写 Store，不自行产生 Run 终态，也不拥有 Resource 的最终释放权。

### 9.7 Capability 子系统

负责 Tool、Skill、Resource、Artifact 和 Memory：

- Tool 定义、参数 Schema、执行和结果标准化；
- Skill Manifest、版本、Tool 需求和权限边界；
- Resource 的按需获取和逆序释放；
- Artifact/Attachment 的引用与内容隔离；
- 可选 Memory 检索和上下文注入。

### 9.8 状态与持久化子系统

负责 Run 聚合的一致提交、有序 Event、Checkpoint、Conversation 和 Artifact 元数据。
目标架构应提供高于独立 Store 的事务性 Run Repository。

### 9.9 可观察性子系统

负责结构化日志、Runtime Event、指标和可选 Trace。观察能力不能改变 Run 执行结果，也
不能把敏感完整载荷作为唯一诊断手段。

### 9.10 Adapter 子系统

负责具体技术或厂商集成。企业专用 MTBI、Raft 或其他平台适配应位于明确的 Adapter 或
下游应用包中，不进入通用领域模型。

## 10. Run 生命周期架构

### 10.1 Agent Run 与 Flow Run

一次直接 Agent 执行创建一个 Agent Run；一次 Flow 执行创建一个 Flow Run：

```text
AgentDefinition ──run──► Agent Run

FlowDefinition ───run──► Flow Run
                           ├── AgentInvocation(planner)
                           ├── AgentInvocation(executor)
                           └── AgentInvocation(executor)
```

第一版 Flow 采用一个顶层 Run：

- Flow Run 是状态、取消、等待、预算、事件和最终结果的边界；
- 每次 Agent 调用记录 `invocation_id`、`agent_key`、输入、输出、Usage 和事件；
- AgentInvocation 不是一个可以独立取消和恢复的根 Run；
- Flow 中活动 Agent 进入 WAITING 时，整个 Flow Run 进入 WAITING；
- Flow 取消或中断时，当前 AgentInvocation 和 Flow 一起结束；
- Agent 之间默认隔离消息上下文，只通过 Flow 显式传递结果、Artifact 或摘要；
- Flow Run 汇总所有 AgentInvocation 的模型步数、Tool 次数和 Token Usage。

这种设计比直接在 Flow 中调用 `agent.run()` 更简单，也避免产生多个没有父子关系的根
Run。未来确实需要独立调度、并行恢复或子任务查询时，再扩展 `parent_run_id` 和 Child
Run；不作为第一版 Flow 的前提。

### 10.2 对外状态

```text
CREATED ──► RUNNING ──► COMPLETED
               │       FAILED
               │       CANCELLED
               │       INTERRUPTED
               │       LIMIT_REACHED
               │
               └─────► WAITING ──► RUNNING
                           │
                           ├──────► CANCELLED
                           └──────► INTERRUPTED
```

状态语义：

| 状态 | 含义 |
| --- | --- |
| `CREATED` | Run 已被接受，但尚未开始推进。 |
| `RUNNING` | 某个执行者当前拥有推进权。 |
| `WAITING` | 等待外部输入，存在可用恢复状态。 |
| `COMPLETED` | 成功形成最终输出。 |
| `FAILED` | 执行失败且不能在当前尝试内继续。 |
| `CANCELLED` | 已接受业务取消并安全结束。 |
| `INTERRUPTED` | 执行载体被中断，当前版本不允许恢复。 |
| `LIMIT_REACHED` | 达到模型步数、Tool 次数或其他执行预算。 |

### 10.3 内部阶段

`RUNNING` 是对外状态，不应该承担所有内部流程语义。目标架构另设非公开执行阶段，例如：

```text
INITIALIZING
ROUTING
INVOKING_AGENT
HANDING_OFF
REQUESTING_MODEL
EXECUTING_TOOL
PLANNING
EXECUTING_STEP
REPLANNING
SUMMARIZING
FINALIZING
```

内部阶段用于日志、指标、Checkpoint 和诊断，不扩大公共 RunStatus 状态机。

### 10.4 生命周期所有权

目标架构设置唯一 Run Lifecycle 边界，负责：

- 校验状态转换；
- 生成 Run patch；
- 追加对应 Event；
- 保存或删除 Checkpoint；
- 完成或释放 Conversation Turn；
- 提交后发送通知。

Strategy、Tool 和 HTTP Adapter 只能提出结果或命令，不能直接拼装终态。

### 10.5 取消优先级

1. 已经持久化并被 Runtime 接受的业务取消，以 `CANCELLED` 结束；
2. 没有业务取消时收到 Task `CancelledError`，以 `INTERRUPTED` 结束；
3. 进程无法执行清理代码时，由租约和恢复器判定 Worker 丢失；
4. 取消不能覆盖已经持久化的正常终态。

## 11. Agent、Flow、Model、Tool 与 Plan 编排架构

### 11.1 核心概念关系

```text
FlowDefinition
  ├── agents: dict[agent_key, AgentDefinition]
  └── strategy: FlowStrategy
                     │
                     ├── 选择 Agent
                     ├── 构造 AgentInput
                     ├── 更新 FlowState / Plan
                     └── 处理 AgentOutput

AgentDefinition
  ├── Prompt / Profile
  ├── Provider route
  ├── Tools / Skills / Permissions
  └── AgentStrategy（默认 ModelTool）

Plan
  └── 描述任务 Step；Step 可由 Flow 分配 agent_key
```

四者职责不能混淆：

| 概念 | 核心职责 |
| --- | --- |
| Agent | 使用一套 Prompt、Tool、Skill 和模型策略完成一个角色任务。 |
| Tool | Agent 可调用的原子、类型化能力。 |
| Skill | 注入一个 Agent 的可复用任务知识、Tool 约束和输出约定。 |
| Flow | 协调多个命名 Agent 的选择、顺序、交接和整体完成条件。 |
| Plan | 表达当前任务需要完成哪些 Step 以及依赖、状态和结果。 |
| Run | 记录一次 Agent 或 Flow 执行的生命周期、事件、预算和结果。 |

Skill 解决“一个 Agent 应该怎样完成某类任务”，Flow 解决“多个 Agent 应该怎样分工和
交接”。Skill 不负责选择另一个 Agent，Flow 也不替代各 Agent 自己的 Skill。

### 11.2 Flow 契约

FlowDefinition 至少声明：

- 稳定 Flow ID 和版本；
- `agent_key -> AgentDefinition` 映射；
- FlowStrategy；
- 输入和最终输出约定；
- 总体模型、Tool、时间和调用预算；
- 允许共享的 Artifact、Memory 或业务状态；
- 失败、等待和取消的传播策略。

FlowStrategy 每次只推进一个有界动作，目标决策包括：

```text
InvokeAgent(agent_key, input)
UpdatePlan(plan)
Continue(state)
WaitForInput(pending_input)
Complete(output)
Fail(error)
```

首批内置 Flow 建议只提供：

1. **SequentialFlow**：固定 Agent 阶段管道；
2. **RouterFlow**：由确定性规则或 Router Agent 选择目标 Agent；
3. **PlannerExecutorFlow**：Planner Agent 创建/更新 Plan，Executor Agent 执行 Step。

AgentDemo 中的 `MTBIAnalysisFlow` 对应 SequentialFlow；其 `PlanningFlow` 对应带 Agent
路由的 PlannerExecutorFlow。mock-manus 的 `PlannerReActFlow` 对应固定 Planner Agent
与 ReAct Agent 分工。`FlowFactory` 可以作为应用构造便利层，但核心扩展点应是
FlowStrategy/FlowDefinition，而不是硬编码 Enum。

### 11.3 Agent 调用和上下文交接

Flow 通过 `AgentInvoker` 调用一个 AgentDefinition，而不是调用公开 `Agent.run()`：

```text
FlowContext
  → AgentInvoker.invoke(agent_key, AgentInput)
      → 创建隔离的 AgentInvocationContext
      → 执行该 Agent 的 Model/Tool 循环
      → 返回 AgentOutput
  → FlowStrategy 决定下一步
```

默认交接规则：

- 原始用户输入由 Flow 显式决定是否传给每个 Agent；
- 上一个 Agent 的完整消息历史不自动进入下一个 Agent；
- Flow 传递结构化结果、摘要和 Artifact 引用；
- 每个 Agent 只能访问自己的 Tool、Skill 和权限；
- Resource 按 AgentInvocation 或 Flow 执行段作用域获取；
- Event 必须带 `flow_id`、`run_id`、`invocation_id` 和 `agent_key`；
- Flow 可以共享总预算，也可以为各 Agent 设置子预算。

### 11.4 Agent 内编排决策

AgentStrategy 接口负责一个 Agent 内部的 Model/Tool/ReAct 行为，目标决策包括：

```text
RequestModel
ExecuteToolBatch
UpdatePlan
WaitForInput
Complete
Fail
ReachLimit
```

Runtime 执行决策并将观察结果再次交给 Strategy。FlowStrategy 不直接执行 Tool，
AgentStrategy 不负责选择另一个 Agent。迁移可以逐步进行，不要求一次把现有 Strategy
改造成纯函数。

### 11.5 Model 调用

- ModelProvider 接收 Provider 中立的 ModelRequest；
- Provider 声明 Tool、Attachment、Memory、Structured Output 和 Streaming 能力；
- Provider 错误按认证、限流、超时、上下文超限、无效响应和临时不可用分类；
- 重试策略由 Runtime/Supervisor 决定，不由具体 Provider 隐式无限重试；
- 每次调用关联 Run 和 AgentInvocation，并计入对应预算。

### 11.6 Tool 调用

Tool 调用经过以下边界：

```text
模型提出 ToolCall
  → Tool 是否注册
  → 当前 Agent Profile/Skill 是否启用
  → 权限是否满足
  → 参数校验
  → 副作用/确认策略
  → 超时与执行隔离
  → 结果大小与脱敏
  → ToolResult
```

同步副作用 Tool 不应依赖线程超时实现强制停止。需要硬超时的操作应采用支持协作取消的
异步实现，或放入可终止进程/沙箱。

### 11.7 Plan

Plan 是一个 Run 内的战术执行图：

- Plan 不创建第二套 Run 生命周期；
- Plan 可以被单 Agent Strategy 使用，也可以被 FlowStrategy 使用；
- Flow 使用 Plan 时，Step 可以声明 `agent_key` 和 Agent 内部 `executor`；
- `agent_key` 决定由哪个 Agent 执行，`executor` 决定该 Agent 使用 model/react 等策略；
- Step 使用目标 Agent 自己的 Provider、Tool、Skill 和权限；
- Plan/Step 状态通过 Run Lifecycle 持久化；
- 第一版按确定顺序执行依赖就绪的 Step；
- 并行 Step 只有在副作用、资源和结果合并规则明确后才能启用；
- 大型 Step 结果应转为 Artifact 引用，而不是无限进入 Plan metadata。

## 12. 状态、事件与持久化架构

### 12.1 核心数据对象

| 对象 | 作用 | 权威性 |
| --- | --- | --- |
| AgentDefinition | 一个角色的 Prompt、能力、模型、权限和限制 | Agent 定义 |
| FlowDefinition | 命名 Agent 集合、FlowStrategy 和总体限制 | Flow 定义 |
| Run | 当前执行快照 | 当前状态权威 |
| AgentInvocation | Flow Run 内一次 Agent 调用的输入、输出、状态和 Usage | Flow 子执行记录 |
| RuntimeEvent | 不可变有序历史 | 审计和回放权威 |
| RuntimeCheckpoint | 恢复所需执行状态 | WAITING 恢复权威 |
| Conversation | 多 Turn 容器 | Conversation 归属权威 |
| ConversationTurn | Conversation 与 Run 的顺序关系 | Turn 状态权威 |
| ExecutionPlan | Run 内计划及 Step 状态 | 编排状态 |
| Attachment/Artifact | 输入输出内容引用 | 内容归属引用 |

### 12.2 一致性不变量

1. 每个 Run 只有一个最终状态和一个对应终态事件；
2. Event sequence 在单个 Run 内连续且唯一；
3. `cancel_requested` 只能从 `False` 变为 `True`；
4. 同一个 WAITING Checkpoint 只能被一个恢复操作取得；
5. `WAITING` 必须具备可解释的 PendingInput 和恢复数据；
6. 永久终态不能保留可再次恢复的 Checkpoint；
7. Conversation 终态 Run 对外可见前，其 active Turn 必须已释放；
8. Artifact 内容不进入 Event、Run 或 Checkpoint，只保存受控引用；
9. 旧版本快照不能覆盖更新版本或清除并发控制字段。
10. Flow Run 同一时刻只有一个活动 AgentInvocation，除非 Flow 显式支持安全并行；
11. AgentInvocation 只能使用对应 AgentDefinition 声明的 Tool、Skill 和权限；
12. Agent 之间的消息和 Memory 默认隔离，Flow 只能通过显式交接数据共享；
13. Flow 终态必须与最后一次 AgentInvocation 和 Plan 状态相容。

### 12.3 事务边界

当前独立 Store Port 继续保留为基础能力。目标增加 Run Repository/Unit of Work，支持
以下原子业务操作：

- `create_run`
- `start_run`
- `record_progress`
- `record_agent_invocation`
- `suspend_run`
- `submit_resume`
- `claim_execution`
- `request_cancel`
- `finalize_run`

一次状态提交至少应原子包含：

```text
Run Snapshot/Patch
+ Runtime Events
+ Checkpoint 创建、更新或删除
+ Conversation Turn 变化
+ Artifact 元数据引用
```

大型 Artifact 二进制可以独立写入对象存储，但必须通过临时对象、提交引用和清理策略
处理跨系统一致性。

### 12.4 并发控制

- Run 增加单调 `version`，使用 compare-and-swap 或数据库行锁；
- 更新使用字段级 patch，避免整对象旧快照覆盖并发字段；
- Event sequence 在同一 Run 事务内分配；
- Resume、Cancel、Finalize 必须幂等；
- 外部命令使用 request/idempotency key 防止重复提交。

## 13. Checkpoint 与恢复架构

### 13.1 Checkpoint 范围

Checkpoint 保存：

- schema version；
- Run ID、执行主体类型和 AgentDefinition/FlowDefinition 引用或 hash；
- AgentStrategy/FlowStrategy 类型、版本和私有状态；
- 当前 AgentInvocation、agent_key 和各 Agent 的隔离上下文；
- 消息、模型响应、预算和 Usage；
- Plan 和 PendingInput；
- Tool ActionBatch 的可恢复进度；
- Attachment、Artifact、Memory 和外部 Session 引用。

Checkpoint 不保存：

- 数据库连接；
- Browser、Page、Container、线程或进程；
- Provider SDK Client；
- 未受控的本机绝对路径；
- 二进制文件内容。

### 13.2 恢复等级

| 等级 | 能力 | 当前状态 |
| --- | --- | --- |
| L0 | 无恢复，只形成终态 | 已支持任务中断转 `INTERRUPTED` |
| L1 | WAITING 人工输入恢复 | 已支持 |
| L2 | Worker 丢失后从安全边界恢复 | 目标 |
| L3 | 任意 Model/Tool 中间点透明恢复 | 非目标，除非外部能力提供强幂等 |

### 13.3 版本兼容

Checkpoint 使用版本化 envelope。恢复时必须验证：

- Checkpoint schema 是否可升级；
- Strategy 类型和版本是否兼容；
- AgentProfile/Skill/Tool 集合是否仍满足原执行约束；
- 外部 Resource 引用是否仍有效；
- 不兼容时返回明确错误，不得以当前配置静默继续。

## 14. Tool 副作用和幂等架构

### 14.1 Tool 分类

建议 Tool 显式声明：

| 类型 | 示例 | 自动重试 |
| --- | --- | --- |
| `READ_ONLY` | 搜索、查询 Schema | 可按策略重试 |
| `IDEMPOTENT_WRITE` | 使用业务幂等键更新对象 | 可使用相同键重试 |
| `NON_IDEMPOTENT_WRITE` | 付款、发送、创建无幂等支持的对象 | 默认禁止自动重试 |

### 14.2 执行账本

需要恢复或重试的 Tool 使用 metadata-only 账本。当前实现采用：

```text
(flow_run_id, invocation_id, operation_key)
→ PREPARED / STARTED / CONFIRMED / ABORTED
→ retry_mode + idempotency_key_digest
```

进程在外部副作用完成后、结果提交前消失时，状态继续保留 `STARTED`，它在语义上代表结果
未知。账本不保存 Tool 参数、结果、原始幂等键或异常正文。系统必须查询外部系统、使用同一
幂等键重试或请求人工确认，不能假设“没有保存结果等于没有执行”。

### 14.3 保证边界

base-agent 可以保证内部命令和状态提交的幂等，但无法单独保证外部业务副作用恰好一次。
最终保证取决于 Tool 后端是否支持幂等键、查询和补偿。

## 15. Resource、Artifact 与 Memory 架构

### 15.1 Resource

- Resource 以一次执行段为作用域；
- 同名 Resource 在一个执行段内最多获取一次；
- 获取失败时逆序释放已经获得的 Resource；
- WAITING、终态和中断都结束当前执行段；
- Resume 创建新执行段并重新获取 Resource；
- 跨段连续性通过外部 Session ID 实现。
- Flow 可以声明 Resource 为 Flow 级或 AgentInvocation 级；默认采用更窄的
  AgentInvocation 作用域。

### 15.2 Artifact

- Attachment 是显式选择的输入引用；
- Artifact 是 Run 产生的输出引用；
- 二进制内容通过 ArtifactStore 访问；
- 大型模型或 Tool 结果溢出为 Artifact；
- 宿主应用负责上传、恶意内容扫描、租户归属、下载授权和保留策略；
- 生产环境优先使用对象存储，数据库只保存引用和必要元数据。

### 15.3 Memory

- MemoryRetriever 是可选 Port；
- 初始化检索结果属于本次 Run 上下文；
- WAITING 恢复使用相同选择，不静默重新检索；
- embedding、索引、写回、删除、租户过滤和保留策略不进入 Runtime 核心。

## 16. 部署架构

### 16.1 嵌入式模式

```text
Python Application
└── Agent
    ├── AgentRuntime
    ├── InMemory Stores
    └── Local asyncio Task
```

适用于测试、CLI、Notebook 和单进程应用。进程结束后活动 Run 不具备存活保证。

### 16.2 单机服务模式

```text
Client
  → FastAPI/SSE
      → RunTaskManager
          → AgentRuntime
      → PostgreSQL
      → Redis notification（可选）
```

这是当前可实现的服务形态。PostgreSQL 可以保存状态，但进程内 Task 不会因为使用了
PostgreSQL 就自动获得重启恢复能力。

### 16.3 持久化 Worker 模式

```text
API Service
  → Durable Run Command / Queue
      → Worker Pool
          → Execution Lease
          → AgentRuntime
          → PostgreSQL
          → Object Storage

Recovery Service
  → 扫描过期 Lease
  → 判定恢复、人工处理或 INTERRUPTED
```

目标组件：

- `RunExecutor` Port；
- `LocalRunExecutor` 默认实现；
- Queue/Worker Adapter；
- execution attempt、lease、heartbeat；
- command idempotency 和 outbox；
- Worker 优雅停机和过期租约恢复器。

Queue 负责调度，不是 Run/Event 的事实来源。Worker 必须通过 Repository 取得执行权。

## 17. 并发与高可用设计

### 17.1 并发所有权

- 一个 Run 同一时刻最多有一个有效执行 lease；
- 一个 Conversation 同一时刻最多有一个 active Turn；
- 一个 WAITING 恢复输入只能被接受一次；
- Event sequence 只能由持有 Run 写权的事务分配；
- 重复 Job 投递不得创建第二个 Run 或第二个终态事件。

### 17.2 Worker 失效

Worker 定期续租。租约超时后恢复器：

1. 读取 Run、execution attempt、Checkpoint 和 Tool 执行账本；
2. 判断最后安全边界；
3. 对只读或幂等操作允许新 attempt；
4. 对未知的非幂等副作用进入人工确认；
5. 无可恢复状态时标记 `INTERRUPTED`；
6. 记录恢复决策 Event。

### 17.3 基础设施降级

- Redis 故障：退化为持久 EventStore 轮询；
- Event 通知故障：不回滚已经提交的事实；
- Artifact Store 故障：引用不得提前提交为可用；
- Provider 临时错误：遵守有限重试和总 deadline；
- PostgreSQL 不可用：停止接受需要持久保证的新 Run；
- 观察系统故障：不应直接改变业务终态，但必须有本地错误记录。

## 18. 安全架构

### 18.1 身份与租户

核心不绑定身份厂商，但目标 Port 和数据模型必须允许宿主传递：

- tenant ID；
- subject ID；
- authorization context；
- request correlation ID。

Run、Conversation、Event、Checkpoint、Attachment 和 Artifact 必须具备一致的所有权过滤。
读、流式订阅、恢复、取消和下载都需要授权，且不能通过错误差异泄露其他租户资源存在。

### 18.2 Event 数据策略

目标支持：

- `FULL`：仅限受控本地调试；
- `REDACTED`：保留结构并替换敏感值；
- `METADATA`：只保存类型、状态、计数、耗时和关联标识。

所有模式都保留事件顺序和终态语义。大载荷使用 Artifact 引用，Event 必须有大小限制。

### 18.3 Tool 与 Resource 安全

- 高风险 Tool 需要显式权限和可选人工确认；
- Shell、文件、浏览器、网络和数据权限分别声明；
- Sandbox 限制文件系统、进程、网络和资源配额；
- Browser 私网防护必须把 DNS 策略与实际连接绑定，不能只做连接前检查；
- MCP 和远程 Tool 的认证、TLS 和命名空间由 Adapter/宿主配置；
- Secret 不进入 Prompt、Skill、Profile、Event 或错误详情。

### 18.4 供应链

- 可选依赖按 extra 安装；
- 基础包导入不能隐式加载厂商 SDK；
- 生产镜像锁定依赖并执行漏洞扫描；
- Skill、Tool 和 Adapter 版本进入运行审计元数据。

## 19. 可观察性

### 19.1 关联标识

至少统一：

```text
request_id
conversation_id
turn_sequence
run_id
execution_attempt
flow_id
flow_version
invocation_id
agent_key
model_call_id
tool_call_id
event_sequence
```

### 19.2 日志

- Library 默认使用 `NullHandler`；
- 宿主应用显式配置日志；
- 日志默认不记录 Prompt、Tool 参数、模型正文和 Secret；
- 结构化字段包括阶段、耗时、状态、Provider、Tool 和错误分类。

### 19.3 Event

Event 用于 Run 历史、审计和客户端重放。Event 不是日志替代品，也不承担 Queue 职责。
Redis 只发送已提交 sequence 通知。

Flow 至少增加以下通用事件语义：

```text
flow.started
flow.completed
flow.failed
agent.invocation.started
agent.invocation.completed
agent.invocation.failed
agent.invocation.waiting
```

这些事件描述协作边界，不引入业务 Agent 名称对应的专用事件类型。

### 19.4 Metrics 与 Trace

定义框架无关指标语义：

- Run 数量、状态和持续时间；
- Model 调用、Token、延迟、重试和错误；
- Tool 调用、拒绝、超时、结果大小和未知副作用；
- WAITING 时长、Resume 冲突和 Worker lease 过期；
- Resource 获取、释放和失败。

OpenTelemetry、Prometheus 和具体 Dashboard 由可选 Adapter 或部署工程实现。

## 20. 扩展机制

### 20.1 稳定扩展点

- `ModelProvider`
- `Tool` / `ContextualTool`
- `SkillRegistry`
- `OrchestrationStrategy`
- `FlowStrategy`
- `Supervisor`
- Store/Repository
- `ResourceSpec`
- `ArtifactStore`
- `MemoryRetriever`
- `RunExecutor`

### 20.2 扩展约束

- 扩展不得绕过 Run 生命周期直接伪造终态；
- Tool 不得自行获得未声明权限；
- Strategy 状态必须版本化且可序列化；
- FlowStrategy 只能通过 AgentInvoker 调用已注册 Agent；
- Flow 不得隐式合并不同 Agent 的完整消息历史和权限；
- Adapter 不得把厂商类型放入核心模型；
- 业务 Skill 和领域 Tool 默认留在下游应用；
- 扩展必须提供无真实网络依赖的契约测试替身。

### 20.3 公共 API

根包只应长期保留最常用的稳定入口。高级类型从明确子包导入。公共 API 收缩需要经过
弃用周期和 API snapshot 测试，不能直接破坏下游应用。

## 21. 关键架构决策与取舍

| ID | 决策 | 主要取舍 |
| --- | --- | --- |
| ADR-001 | Agent 开发者体验是首要架构驱动力 | 内核复杂度必须被公共开发 API 隐藏。 |
| ADR-002 | Flow 是 Agent 之上的简单协作层 | 支持常见多 Agent 分工，但不建设自治多 Agent 平台。 |
| ADR-003 | 第一版 Flow 使用单一顶层 Run | 生命周期简单，但 AgentInvocation 不能独立调度。 |
| ADR-004 | 核心采用 Library First | 保持轻量，但生产能力需要宿主或 Adapter 组合。 |
| ADR-005 | Run 是执行聚合 | 统一状态和审计，但需要明确事务边界。 |
| ADR-006 | Runtime 生命周期单一写入 | 降低不一致风险，但 Strategy 自由度受约束。 |
| ADR-007 | Event 先持久化后通知 | 支持重放，但实时通知可能短暂延迟。 |
| ADR-008 | Resource 按执行段管理 | 清理可靠，但 WAITING 后需要重新连接。 |
| ADR-009 | 当前 `INTERRUPTED` 不可恢复 | 避免不安全重放，牺牲自动续跑能力。 |
| ADR-010 | Plan 属于 Run | 共享预算和事件，但不是通用工作流引擎。 |
| ADR-011 | Redis 不作为事实源或任务队列 | 降低一致性复杂度，持久队列需独立实现。 |
| ADR-012 | Tool 幂等依赖执行账本和后端能力 | 无法承诺任意外部副作用恰好一次。 |
| ADR-013 | 身份由宿主提供、所有权由 Port 传递 | 不绑定厂商，但核心接口必须支持租户上下文。 |

## 22. 当前实现差距

| 领域 | 当前状态 | 目标差距 | 优先级 |
| --- | --- | --- | --- |
| Agent 定义体验 | 已支持版本化 AgentDefinition、兼容 AgentProfile、Tool、Skill 和组合 | 缺少文件化 Prompt 加载和更清晰的推荐目录 | P0 |
| Agent 测试体验 | 已有 FakeModel、ToolHarness、SkillHarness、AgentTestHarness | 已支持完整 Run、Event、ModelRequest 和 WAITING/resume 场景证据；后续按真实应用需求扩展断言约定 | P0 |
| 能力复用 | 已有 Toolkit、Bundle 和 Skill | 缺少稳定的能力包契约、发现方式和版本策略 | P0 |
| Flow | 已有 FlowDefinition、显式 Handoff、SequentialFlowStrategy、AgentRuntimeInvoker、FlowBudget、主动取消传播、FlowTestHarness 和脚本化测试替身 | 增加耐久执行、PlannerExecutorFlow 和 RouterFlow | P0 |
| Flow Run | 已有不可变状态、CAS Repository、lease、worker、恢复策略、Operator review 和副作用账本 | 增加持久 Checkpoint 恢复策略 | P0 |
| Plan 分工 | Plan Step 的 executor 只表达 model/react | 分离 `agent_key` 与 Agent 内部 `executor` | P1 |
| 开发文档 | 专题文档较完整 | 主路径被运行时细节分散，需要面向 Agent 作者重新组织 | P0 |
| Run 生命周期 | Runtime、Strategy、Agent 共同参与 | 收口到统一生命周期边界 | P0 |
| 持久化 | Agent Runtime 使用独立 Store；Flow 已有内存与 PostgreSQL 原子 Repository、连续事件、revision/CAS 和 lease fencing | 将统一 Repository/UoW 扩展至 Agent Run，并补齐 Worker 恢复与副作用边界 | P0 |
| Event 安全 | 可能保存完整敏感正文 | FULL/REDACTED/METADATA 与大小限制 | P0 |
| Browser 网络 | 存在 DNS 检查与实际连接分离风险 | 受控代理或连接级地址绑定 | P0 |
| HTTP 租户 | 由宿主自行处理，模型无统一所有权字段 | 认证上下文和全链路所有权过滤 | P0 |
| Task 中断 | 已收敛为不可恢复 INTERRUPTED | 保持行为并完成兼容提交 | P0 |
| Strategy | 可直接写 Store 和状态机 | 类型化决策、版本化私有状态 | P1 |
| Checkpoint | 支持 WAITING，无 schema envelope | schema/strategy/definition 版本 | P1 |
| Worker | Flow 已有 fenced lease、PostgreSQL work source、polling worker、保守 RecoveryPolicy、Operator review 和副作用恢复门禁 | 增加并发策略 | P1 |
| Tool 副作用与结果 | 已有显式分类、类型化确认、Flow 账本、幂等键和统一 UTF-8 结果上限 | 增加认证 Operator transport 和显式 Artifact overflow policy | P1 |
| Provider | 基础协议和 Adapter | 能力声明、错误分类和有限重试 | P1 |
| 可观察性 | Event、日志和 Token Usage | Metrics、Trace、Conversation Event | P1 |
| 公共 API | 根包导出较多 | 稳定最小入口和弃用策略 | P2 |
| 数据运维 | PostgreSQL 可创建 Schema | Migration、Retention、Backup、Health | P2 |

## 23. 分阶段实施计划

### 阶段 A：确立 Agent 开发主路径

1. 定义推荐的 Agent 应用目录：Prompt、Tools、Skills、Profile、测试和 composition root；
2. 提供一个只包含 Prompt 和一至两个 Tool 的最小 Agent 模板；
3. 提供包含 Skill、Resource 和可选 Plan 的标准模板；
4. 统一 Prompt 加载、Tool/Skill 注册、Profile 启用和权限配置方式；
5. 增加完整 Agent 场景测试 Harness 和错误诊断；
6. 把 Getting Started 重组为“定义 → 运行 → 测试 → 扩展 → 部署”。

完成标准：新同事不阅读 Runtime 源码，也能在较短时间内完成一个离线可测试的业务
Agent。

### 阶段 B：一致性和安全基线

1. 固化并提交 `INTERRUPTED` 行为及回归测试；
2. 设计并实现 Event 数据安全策略和载荷上限；
3. 修复 Browser DNS rebinding 风险；
4. 为 Run 增加 version/CAS，建立状态与终态不变量测试；
5. 定义租户/主体上下文 Port，不立即绑定身份厂商。

完成标准：当前单机能力不存在已知 P1 状态损坏或高风险网络绕过。

### 阶段 C：生命周期与事务收口

1. 引入统一 Run Lifecycle；
2. 引入 Run Repository/Unit of Work；
3. Strategy 停止直接写 RunStore、EventStore 和 CheckpointStore；
4. Conversation 收尾、Checkpoint 和终态事件进入同一业务提交；
5. 保持现有 Agent API 兼容。

完成标准：任何 Run 状态变化只有一个写入路径，故障注入不能产生矛盾状态。

### 阶段 D：建立简单 Flow 能力

1. 复用已从 Agent 门面中提取的不可变 AgentDefinition；
2. 定义 FlowDefinition、FlowStrategy、FlowContext 和 FlowResult；
3. 引入 AgentInvoker，在同一个 Flow Run 中执行隔离的 AgentInvocation；
4. 增加 Flow/AgentInvocation 事件、Usage 聚合、取消和 WAITING 传播；
5. 实现 SequentialFlow 和 PlannerExecutorFlow，RouterFlow 按实际需求增加；
6. 为 PlanStep 分离 `agent_key` 和 Agent 内部 `executor`；
7. 提供 FlowHarness、脚本化 Agent 和固定两阶段示例。

完成标准：两个使用不同 Prompt、Tool 和权限的 Agent 可以在一个 Flow Run 中顺序协作，
不复制 Runtime 逻辑，且能完全离线测试。

### 阶段 E：扩展契约与编排规范化

1. 稳定 Tool、Skill、Toolkit、Provider、Resource、AgentStrategy 和 FlowStrategy 契约；
2. 引入类型化 Strategy Decision；
3. 拆分公开 RunStatus 和内部执行阶段；
4. 引入版本化 Checkpoint envelope；
5. 规范 Plan、ReAct 和 ActionBatch 私有状态；
6. 对大型 Tool/Step 结果实施 Artifact overflow。

完成标准：普通 Agent 通过组合扩展；高级 Strategy 无须了解 Store 顺序；旧 Checkpoint
有明确兼容结果。

### 阶段 F：生产执行边界

1. 定义 `RunExecutor` Port 和 Local 实现；
2. 增加 command/outbox、execution attempt、lease 和 heartbeat；
3. 增加通用 Queue/Worker Adapter；
4. 实现 Worker 丢失检测和安全恢复策略；
5. 增加重复投递、优雅停机和进程强杀测试。

完成标准：API 和 Worker 可独立重启，重复投递不会产生第二终态或静默重复副作用。

### 阶段 G：生产治理

1. Provider 错误分类、重试和能力声明；
2. Tool 副作用分类、确认和执行账本；
3. Metrics、Trace 和 Conversation Event；
4. 数据库 Migration、Retention、Backup 和 Health；
5. 公共 API 收缩及 Adapter 命名空间整理。

完成标准：具备可运维、可升级、可审计的生产部署基线。

## 24. 架构验收标准

### 24.1 Agent 开发体验

- 一个简单 Agent 主要由 Prompt、AgentProfile、Tool 和 Skill 构成；
- 增加业务 Agent 不需要继承或修改 AgentRuntime；
- 增加普通 Tool 不需要理解 Run、Event、Checkpoint 或 Store；
- Prompt、Tool、Skill、权限和模型配置拥有单一推荐组织方式；
- Starter 能够作为业务项目模板复制，而不是 Runtime 的另一套实现；
- 配置错误在模型调用前形成可定位、可操作的错误信息。

### 24.2 Flow 开发体验

- 两个已定义 Agent 可以通过固定阶段 Flow 组合，不需要修改 AgentRuntime；
- Flow 通过 agent key 选择 Agent，不从 Prompt 文本中隐式猜测对象；
- Planner/Executor Flow 可以把 Plan Step 分配给指定 Agent；
- 每个 Agent 保持独立 Prompt、消息、Tool、Skill 和权限；
- Agent 之间只共享 Flow 明确声明的结果、摘要和 Artifact；
- 一个 Flow 执行形成一个可查询、可取消、可等待恢复的顶层 Run；
- FlowHarness 可以验证 Agent 调用顺序、交接输入、失败传播和最终结果。

### 24.3 测试与能力复用

- Tool、Skill、Agent 和 Flow 均可离线、确定性测试；
- 通用 Tool 可以组成 Toolkit，由不同 Profile 显式选择；
- Skill 的版本、Tool 依赖、权限和输出约定可验证；
- 从内存 Provider/Store 切换到真实 Adapter 不改变业务 Prompt、Tool 和 Skill；
- 示例和测试不要求真实 API Key。

### 24.4 依赖与扩展

- 基础包不安装可选依赖也能导入和运行；
- Runtime 不导入 FastAPI、SQLAlchemy、Redis、Docker、Playwright 或厂商 SDK；
- 新 Provider、Tool、Skill 和 Store 可通过契约实现，无须修改核心循环；
- 依赖方向由自动化测试或 import-linter 保护。

### 24.5 生命周期与一致性

- 每种终态均有唯一对应终态事件；
- Run、Event、Checkpoint 和 Conversation 的故障注入测试覆盖每个提交边界；
- 并发 Cancel、Resume、Finalize 不丢状态、不产生双执行；
- Flow Run 和当前 AgentInvocation 状态相容，不产生游离 Agent 执行；
- Task 取消和 Worker 进程丢失都不会永久遗留无法解释的 `RUNNING`；
- Redis 或 SSE 故障不损坏持久事件历史。

### 24.6 安全

- 未授权 Tool 在执行前被拒绝；
- Flow 调用 Agent 时不能扩大该 Agent 的权限或 Tool 集合；
- `METADATA` Event 模式不包含 Prompt、Tool 参数、结果或 Secret；
- 一名租户不能读取、流式订阅、恢复、取消或下载其他租户资源；
- Browser 私网策略覆盖跳转和 DNS rebinding；
- 非幂等副作用在未知结果状态下不会被自动重试。

### 24.7 恢复与高可用

- WAITING Run 可以在进程重启后恢复；
- 持久化 Worker 模式下 API 重启不丢失已接受任务；
- Worker 租约过期后可以确定选择恢复、人工处理或 `INTERRUPTED`；
- 重复 Job 和重复恢复请求具有稳定幂等结果。

### 24.8 可观察性和运维

- 一个 Run 可以通过稳定 ID 关联日志、Event、指标和 Trace；
- 所有 Event 载荷和 Tool 结果具有可配置大小上限；
- 数据库升级使用版本化 Migration；
- 服务提供存活与依赖就绪检查；
- 保留和清理任务不会删除活动或可恢复状态。

## 结论

`base-agent` 首先是 Agent 应用开发框架，其价值是让其他开发者把精力放在 Prompt、
Tool、Skill、Agent 分工和简单 Flow 上，而不是重复开发 Runtime。统一执行内核是实现
这个目标的手段，不是框架对普通开发者暴露的中心。

近期工作应按以下顺序展开：

1. 固定简单 Agent 的定义、组合、运行和测试主路径；
2. 把 Run 生命周期、事务一致性和安全复杂度收进内核；
3. 在统一生命周期上建立命名 Agent、AgentInvoker 和简单 Flow；
4. 稳定 Tool、Skill、Toolkit、AgentStrategy 和 FlowStrategy 契约；
5. 最后按实际部署需求增加持久化 RunExecutor、Worker lease 和副作用恢复。

最终判断架构是否成功的首要标准不是支持了多少基础设施，而是：

> 一个不了解 base-agent 内部 Runtime 的同事，能否只通过 Prompt、Tool、Skill、
> Agent 配置和简单 Flow，快速实现并可靠测试一个单 Agent 或多 Agent 应用。
