# base-agent next

`next/` 是按 Agent SDK 产品目标重新实现的独立内核，不是旧 Runtime 的目录搬迁。

开发者主路径只有：

```text
Prompt + Tool + Skill
        ↓
 AgentDefinition + Agent
        ↓
   Harness（自动运行、观察、验证、治理）
        ↓
 RunResult
```

简单多 Agent 应用在此之上使用声明式 `SequentialFlow`，不直接接触生命周期仓库、
Invoker、Worker 或恢复状态机。

需要由应用持有可观察 Plan、逐步执行并在每一步后重新判断剩余工作时，使用固定角色的
`PlannerReActFlow`。它组合多个普通 Agent Run，不修改 `Agent.run()` 或 Harness 语义。

## 最小示例

```python
from base_agent import Agent, AgentDefinition
from base_agent.testing import FakeModel

agent = Agent(
    AgentDefinition(
        id="assistant",
        version="1.0.0",
        instructions="Answer clearly and briefly.",
    ),
    model=FakeModel.text("Hello."),
)

result = await agent.run("Hi")
print(result.output)
```

普通 `Agent.run()` 可以通过一次性的 `on_event` 实时观察 Harness Event；这与 Provider
是否支持模型输出 streaming 相互独立：

```python
def on_event(event):
    print(event.sequence, event.kind)

result = await agent.run("Hi", on_event=on_event)
```

支持 streaming 的 Provider 使用 `Agent.stream()`，同一个 Observer 还会收到
`message.delta`，结束后仍返回完整 `RunResult`：

```python
def on_event(event):
    if event.kind == "message.delta":
        print(event.payload["delta"], end="", flush=True)

result = await agent.stream("Hi", on_event=on_event)
```

需要让 Run 在当前进程的后台执行时，使用 `Agent.start()`。它立即返回 `RunHandle`，不等待
模型或 Tool 完成：

```python
from base_agent import RunStatus

handle = agent.start("Hi")
print(handle.run_id)
assert handle.status is RunStatus.RUNNING

current_events = await handle.events()
newer_events = await handle.events(after_sequence=3)
result = await handle.wait()
```

运行中的后台 Run 可以显式取消。取消会等待清理完成，并返回带有
`RunStatus.CANCELLED` 的终态结果：

```python
cancelled = await handle.cancel()
assert cancelled.status is RunStatus.CANCELLED
```

`events()` 返回调用时已经记录的只读 Event 快照，`after_sequence=N` 只返回 sequence 大于
N 的事件。需要先回放历史事件、再持续等待新事件时，使用进程内订阅：

```python
last_sequence = 0
async for event in handle.stream(after_sequence=last_sequence):
    last_sequence = event.sequence
    print(event.kind)
```

订阅在 Run 结束且已发送全部事件后自动结束，多个订阅者互不影响；取消某个订阅不会取消
Run，只有显式调用 `handle.cancel()` 才会取消后台执行。对已取消 Run 重复调用是幂等的；
已经成功或失败的 Run 不能改写为取消。当前 `RunHandle` 不提供持久化；HTTP SSE 已复用同一
事件游标语义实现。
支持 streaming 的 Provider 可通过
`agent.start("Hi", stream=True)` 在后台执行。

Harness、RunResult 和 RunHandle 共享同一个 `EventStore`，默认使用
`InMemoryEventStore`，不会重复保存 Event。需要跨进程持久化时可以安装 `postgres` extra 并
显式使用 `base_agent.events.postgres.PostgresEventStore`；它提供原子 sequence、游标回放和
polling 实时订阅。完整契约和当前 HTTP Registry 边界见 [`EVENTS.md`](EVENTS.md)。

## Planner–ReAct Flow

Planner 和 Executor 都是普通 Agent，但必须声明各自的结构化输出。Planner 只返回 Host
可以校验和提交的 `PlannerDecision`；Executor 在自己的 Run 内正常使用 Tool/Skill，最终返回
`StepOutcome`：

```python
from base_agent import (
    Agent,
    AgentDefinition,
    Flow,
    PlannerDecision,
    PlanningLimits,
    StepOutcome,
)

planner = Agent(
    AgentDefinition(
        id="planner",
        version="1.0.0",
        instructions="Create and review concise executable Plans.",
        response_model=PlannerDecision,
    ),
    model=planner_model,
)
executor = Agent(
    AgentDefinition(
        id="executor",
        version="1.0.0",
        instructions="Execute exactly one Plan Step.",
        tools=("search",),
        response_model=StepOutcome,
    ),
    model=executor_model,
    tools=(search,),
)
flow = Flow.planner_react(
    id="research",
    planner=planner,
    executor=executor,
    limits=PlanningLimits(max_steps=12, max_revisions=4, max_child_runs=25),
)

result = await flow.run("Research the topic and produce a checked answer")
```

这两个内置模型按 OpenAI strict JSON Schema 设计，所有属性都会出现在 `required` 中；与当前
action 无关的值使用 `null`、空数组或空字符串。基础 `StepOutcome.output` 是 `str | None`，
需要领域对象时可定义 `StepOutcome` 子类并把 `response_model` 指向该子类。

状态机固定为 `PLANNING → EXECUTING → REVIEWING`。Review 可以保留剩余步骤、替换尚未执行的
后缀、完成或失败；已经执行的前缀不能被 Planner 改写。Plan、Step 和 attempt id 都由 Host
生成，模型不能生成 `agent_run_id`。

每个 Planner/Executor 调用都通过 `Agent.start()` 形成独立子 Run。`FlowChildRun` 显式记录
`flow_run_id`、`plan_id`、source/target revision、`step_id`、attempt 和 `agent_run_id`；Flow
Event 的 `planner.started`、`step.started` 也携带同一个 `agent_run_id`。子 Run 的
`model.*`、`tool.*` 和 `message.*` Event 保留在子 Run 自己的 EventStore 历史中，不复制到
Flow Event。应用也可以在构造 Flow 时设置 `forward_child_events=True`：子 Run 的
`model.*`、`tool.*`、`skill.*` 和 `context.*` 保留原 kind 与原 payload 字段，追加
`event_scope=child_run`、角色、阶段、关联 id 和 child sequence；内部 `run.*`、`message.*`
使用 `child.run.*`、`child.message.*`，避免与 Flow 终态和用户可见消息混淆。Flow sequence
仍是回放与 SSE 的唯一游标。

后台执行提供同样的回放、实时订阅和取消入口；Flow 取消会继续取消当前活动的子 Run：

```python
handle = flow.start("Research the topic")
current_events = await handle.events()
result = await handle.wait()

another_handle = flow.start("Research another topic")
cancelled = await another_handle.cancel()
```

需要让 Planner 和 Executor 都使用 Provider streaming 时，向 `run()` 或 `start()` 传入
`stream=True`。结构化 JSON 分片会以 `child.message.delta` 进入 Flow Event stream，完整输出仍在
校验后作为 Flow `message.completed` 发布：

```python
handle = flow.start("Research the topic", stream=True)
```

需要实时跟随时可独立迭代 `handle.stream(after_sequence=...)`；多个订阅者互不影响，stream 在
`flow.succeeded`、`flow.failed` 或 `flow.cancelled` 后自然关闭。

Planner 与 Executor 的 Memory scope 必须分别通过 `planner_memory_scope` 和
`executor_memory_scope` 显式传入。Plan 是 Flow 领域状态，不会作为 Memory 或 Message
隐式共享。`FlowRunHandle` 和执行 Task 仍然属于启动它的进程；HTTP 读取层可以通过共享
EventStore 在 Registry miss 时恢复 Plan、终态和事件访问，但跨进程取消、崩溃续跑和工作分配
仍需要共享 FlowRunStore/RunStore、owner lease 与 checkpoint。

## ContextPolicy

Harness 在每次构造 `ModelRequest` 前，把冻结的 SystemContext、Run 内原始 transcript 和可选
MemorySnapshot 交给 ContextPolicy，并只把返回的 request-only
`ContextView.system_prompt/messages` 发送给 Provider。默认
`KeepAllContextPolicy` 保持全部历史；应用可以从 `base_agent.context` 引入扩展契约：

```python
from base_agent.context import (
    ContextBudget,
    KeepAllContextPolicy,
    SystemContextBlock,
)

agent = Agent(
    definition,
    model=model,
    context_policy=KeepAllContextPolicy(),
    system_context_blocks=(
        SystemContextBlock(
            id="company-policy",
            content="Follow the approved company terminology.",
            required=True,
        ),
    ),
    context_budget=ContextBudget(
        context_window_tokens=128_000,
        reserved_output_tokens=8_000,
        safety_margin_tokens=2_000,
    ),
)
```

`ContextBudget` 向策略提供输入上限，但默认 KeepAll 策略不会假装进行 token 估算或裁剪。需要
窗口、Tool result 清理或摘要的应用应实现自己的 `ContextPolicy.select()`，返回
`ContextView` 和不含正文的 `ContextTrace`。核心 instructions 不可修改，required system
block 不可省略，optional block 可以由 Policy 选择；最终 `ContextView.system_prompt` 必须由
这些已声明 blocks 渲染，不能注入 Memory 或任意 system 文本。策略只能选择本次请求，不得
修改 transcript；Memory 候选通过 `ContextInput.memory` 单独提供，模型摘要仍属于后续独立接口。

### Memory 占位与作用域

Memory read/write 默认分别使用 `DisabledMemoryProvider` 和 `DisabledMemoryWriter`，不会执行
召回、写入或产生额外 Memory Event。启用 Memory 必须由应用在每次 Run 传入明确 scope：

```python
from base_agent.memory import MemoryScope

result = await agent.run(
    "Hi",
    memory_scope=MemoryScope(("tenant-42", "user-7")),
)
```

自定义 `MemoryProvider.recall()` 每个 Run 最多调用一次，返回 versioned `MemoryRecord`；同一
MemorySnapshot 会被所有 Model Turn 复用。ContextPolicy 负责把选中的 record 渲染为有边界的
request-only Message，并在 ContextTrace 中报告 `selected_memory_ids`。默认 KeepAll Policy
不会自动注入 Memory。默认 recall limit 为 5、超时为 2 秒，均在 Agent 构造时配置并进入
HarnessSnapshot/fingerprint。

`MemoryWriter.enqueue()` 只接收成功 Run 的完整 Message transcript，并要求实现方立即入队、
不能在调用线程执行数据库、embedding 或模型 I/O。当前只固定该窄接口；Memory 抽取、冲突
合并、遗忘、后台重试、Redis/PG Adapter 和跨 Session 策略后续实现。

内置 HTTP Run Server 暂不从请求 body 接受 MemoryScope，避免客户端自行声明 tenant/user
身份。启用 Memory 的 HTTP 应用以后应从认证上下文注入 scope。

## Artifact 与 Resource

Resource 是 Run 期间存活的能力，Artifact 是 Run 提交的不可变内容，两者不能用一个
workspace 路径代替。应用在构造 Agent 时注册 Resource 和共享 ArtifactStore；Tool 声明
`ToolContext` 参数后，Harness 会自动注入它，但这个参数不会出现在模型看到的 JSON Schema：

```python
from contextlib import asynccontextmanager
from base_agent import Agent, LocalArtifactStore, ResourceSpec, ToolContext, tool

@asynccontextmanager
async def workspace_resource(context):
    workspace = await open_workspace(context.run_id)
    try:
        yield workspace
    finally:
        await workspace.close()

@tool
async def write_report(content: str, context: ToolContext):
    workspace = await context.resources.get("workspace")
    await workspace.write_text("report.md", content)
    return await context.artifacts.create(
        name="report",
        media_type="text/markdown",
        content=content,
        location="report.md",
    )

agent = Agent(
    definition,
    model=model,
    tools=(write_report,),
    artifact_store=LocalArtifactStore(artifact_root),
    resources=(ResourceSpec("workspace", workspace_resource, eager=True),),
)
```

`InMemoryArtifactStore` 用于测试和单进程临时应用；`LocalArtifactStore` 原子提交内容和 JSON
元数据，并在读取时重新校验 size/SHA-256。`RunResult.artifacts` 返回本 Run 产生的 Artifact，
包含 producer Run/Agent、Flow/Stage/attempt、Tool call、源 Artifact ID 和安全元数据。

Sequential Stage 通过 `artifacts=(ArtifactSpec(...),)` 声明输出契约。框架先检查 required、
media type 和大小，再运行领域 verifier；只有全部通过的 Artifact 才会被显式授权给依赖它的
下游 Stage。`FlowResult.artifact_bindings` 同时保留成功和失败 attempt 的绑定结果，
`FlowResult.artifacts` 只聚合已接受的不可变引用。完整边界见
[`docs/adr/0001-artifact-resource-boundary.md`](docs/adr/0001-artifact-resource-boundary.md)。

Planner-ReAct 使用动态选择语义：Tool 创建 Artifact 后，Harness 会把紧凑的
`artifacts_created` receipt 放进对应 Tool observation；Executor 在结构化
`StepOutcome.artifact_ids` 中选择正式 Step 输出。Host 只接受由当前 Executor Run、当前 Flow
和当前 Step attempt 创建的 ID，并发出 `artifact.selected`。未选择的候选 Artifact 仍保留在
producer `RunResult` 和 Store 中用于审计，但不会进入 `PlannerReActResult.artifacts`，也不会被
授权给后续 Run。Review Planner 只在 prompt 中看到所选 Artifact 的元数据；后续 Executor Run
才获得读取授权。当前实现会把此前所有已选择输出授权给后续 Executor，精确到 Step 的输入选择器
属于后续扩展。

需要跨 Stage 或 Executor Run 读取 Artifact 时，相关 Agent 必须显式使用同一个 ArtifactStore；
框架在第一次跨 Store 授权时会直接失败，避免产生看似有效但无法读取的引用。

Tool observation 预算由 `AgentDefinition.max_tool_observation_bytes` 和
`max_run_observation_bytes` 控制，默认分别为 16 KiB 和 64 KiB。应用可以针对大型 metadata
Tool 提高预算；这两个值会进入 HarnessSnapshot/fingerprint。单次超限内容仍会被截断，整 Run
累计超限会返回结构化的 `tool_observation_budget_exceeded` failure。

## 可选 HTTP Run Server

HTTP 服务属于 `base-agent` 的可选 `server` 能力，不由 Starter 实现。安装依赖：

```bash
uv sync --project next --extra server --group dev
```

普通 Agent 应用可以直接创建 FastAPI app：

```python
from base_agent.server import create_http_app

app = create_http_app(
    agent,
    flows={"plan-react": planner_react_flow},
)
```

只有 Flow 的应用不需要注册一个无意义的默认 Agent。`AgentApplication` 同时收敛命名 Flow、
启动 preflight 和 FastAPI 组合：

```python
from base_agent.server import AgentApplication

app = AgentApplication(
    flows={"analysis": analysis_flow},
    preflight=(check_dependencies,),
    title="Analysis Service",
).fastapi()
```

`SequentialFlow` 的 Stage 可以声明 `RetryPolicy(max_attempts=3)`。仅当 Agent Run 本身成功、
但 Host verifier 拒绝结果时才重试；框架把 `VerificationEvidence` 写入下一次 Run 的输入，保留
相同 `flow_run_id`，并为每次尝试创建独立 `agent_run_id`。`ArtifactSpec` 由 Host 声明；Tool
提交的不可变 Artifact 经过契约和领域验证后进入 FlowResult、Event 和 HTTP 响应，模型无需
在结构化输出中复述路径。

再由任意 ASGI Server 启动，例如 `uvicorn your_application:app`。当前接口包括：

| Method | Path | Behavior |
| --- | --- | --- |
| `POST` | `/v1/runs` | 后台启动 Run，立即返回 `202`。 |
| `GET` | `/v1/runs/{run_id}` | 返回 Run 当前状态或最终结果。 |
| `POST` | `/v1/runs/{run_id}/cancel` | 取消运行中的 Run；已完成 Run 返回 `409`。 |
| `GET` | `/v1/runs/{run_id}/events` | 按 sequence 游标回放 Event JSON。 |
| `GET` | `/v1/runs/{run_id}/events/stream` | 先回放、再通过 SSE 实时跟随。 |
| `POST` | `/v1/flows/{flow_name}` | 后台启动已注册 Flow，立即返回 `202`。 |
| `GET` | `/v1/flows/{flow_run_id}` | 返回 Flow 状态、Plan 和子 Run 关联。 |
| `POST` | `/v1/flows/{flow_run_id}/cancel` | 取消 Flow 及当前活动子 Run。 |
| `GET` | `/v1/flows/{flow_run_id}/events` | 回放 Flow Event 和可选的关联子 Run Event。 |
| `GET` | `/v1/flows/{flow_run_id}/events/stream` | 通过 SSE 回放并跟随 Flow。 |

SSE 帧把 `Event.sequence` 同时作为 `id` 和断线续传游标：

```text
id: 8
event: run.succeeded
data: {"sequence":8,"kind":"run.succeeded","run_id":"...","turn":1,"payload":{...}}
```

客户端可用 `?after_sequence=5`、`?last_event_id=5` 或 `Last-Event-ID: 5` 只接收 sequence
大于 5 的 Event；Header 优先于 `last_event_id`，再与 `after_sequence` 取较大值。连接空闲时
默认每 15 秒发送一次 SSE comment 心跳，Run/Flow 终态事件发出后流自然关闭。Run 与 Flow
registry 仍只保存当前进程的活动 Handle；配置共享 EventStore 后，重启或请求落到另一 Worker
时，状态、历史和 SSE 会从持久 Event 投影，不再因为 Registry miss 直接返回 404。只有拥有
活动 Handle 的进程可以取消执行；崩溃检测、断点续跑、分布式执行所有权和自动保留期仍需要
共享 RunStore/FlowRunStore 与 lease/checkpoint 机制。

Planner-ReAct 使用与普通 Agent 相同的 `message.created` 表示原始用户消息，并在成功时发送
`message.completed`（payload 含最终 `content`），随后发送 `flow.succeeded` 关闭 SSE。启用
`forward_child_events` 后，客户端还可在同一条流中按原有 `model.*`、`tool.*`、`skill.*`
kind 观察 Planner/Executor，同时通过 payload 的 `event_scope` 和 `agent_run_id` 关联来源。
`POST /v1/flows/{flow_name}` 的 JSON body 接受 `stream` 布尔值；它控制子 Agent Provider
streaming，而 SSE Event 订阅能力始终存在。

Tool、Skill 和 Flow 的完整离线示例见 `examples/quickstart.py`。
Skill Package 的目录、Tool 贡献、Script 执行边界和 snapshot 契约见
[`SKILLS.md`](SKILLS.md)。

模块职责、执行不变量和后续迁移顺序见 [`ARCHITECTURE.md`](ARCHITECTURE.md)，旧实现的
迁入门槛见 [`MIGRATION.md`](MIGRATION.md)。

当前 Provider 契约、OpenAI-compatible Adapter、错误分类和验收命令见
[`PROVIDERS.md`](PROVIDERS.md)。

需要查看通用组合方式和逐步能力示例时，使用独立下游参考项目
[`starter/`](starter/README.md)。需要查看真实领域应用如何只依赖公共 API 时，使用与 Starter
平级的 [`mtbi-analysis/`](mtbi-analysis/README.md)：它组合固定查询/分析 Agent、真实
`mtbi-cli` Adapter、workspace 产物校验和 SequentialFlow，不导入 Starter 或 Harness 内部模块。

## 当前边界

首个纵切只实现：

- 不可变 AgentDefinition；
- 类型化 Tool、Toolkit 和 readiness preflight；
- 由受控目录自动发现、受信任 `tools.py` 加载、SkillDescriptor、SkillCatalog 和
  `skill_view` 渐进加载的
  Skill Package，可携带
  Tool/Toolkit、scripts/references/templates/assets 清单和包指纹；
- 固定的 HarnessSnapshot；
- 以 Model Turn 和单轮 ToolCall 上限约束的 model/tool loop；
- Tool 参数、权限、超时和确认治理；
- Provider-neutral text/ToolCall streaming 与完整响应聚合；
- 进程内 `Agent.start()`、`RunHandle`、显式取消、Event 快照回放和实时订阅；
- 默认内存 `EventStore`、可替换持久历史和可选 `EventStream` 契约；
- 可选 `base_agent.server` FastAPI Run 提交、状态查询与取消；
- 有序只读 Event；
- 结构化最终输出验证；
- 支持 ArtifactSpec/ArtifactStore、验证证据反馈重试和显式阶段交接的 SequentialFlow；
- 固定 Planner/Executor 的 Planner–ReAct Flow、结构化 Plan/Step outcome、逐步 Review/Replan、
  子 Run 关联、事件回放和取消传播。

SDK Runtime 核心明确不依赖 HTTP Server、Worker、Lease、持久化恢复、Operator API、
插件市场、动态 Tool Search、复杂多 Agent 调度或产品级会话路由。FastAPI Run Server 位于
`base_agent.server` 可选边界，只有安装 `base-agent-next[server]` 并显式导入时才会加载。
