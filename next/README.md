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
`InMemoryEventStore`，不会重复保存 Event。持久化与实时扩展契约以及 PostgreSQL/Redis
方向见 [`EVENTS.md`](EVENTS.md)。

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

## 可选 HTTP Run Server

HTTP 服务属于 `base-agent` 的可选 `server` 能力，不由 Starter 实现。安装依赖：

```bash
uv sync --project next --extra server --group dev
```

应用构造好 Agent 后，直接创建 FastAPI app：

```python
from base_agent.server import create_http_app

app = create_http_app(agent)
```

再由任意 ASGI Server 启动，例如 `uvicorn your_application:app`。当前接口包括：

| Method | Path | Behavior |
| --- | --- | --- |
| `POST` | `/v1/runs` | 后台启动 Run，立即返回 `202`。 |
| `GET` | `/v1/runs/{run_id}` | 返回 Run 当前状态或最终结果。 |
| `POST` | `/v1/runs/{run_id}/cancel` | 取消运行中的 Run；已完成 Run 返回 `409`。 |
| `GET` | `/v1/runs/{run_id}/events` | 按 sequence 游标回放 Event JSON。 |
| `GET` | `/v1/runs/{run_id}/events/stream` | 先回放、再通过 SSE 实时跟随。 |

SSE 帧把 `Event.sequence` 同时作为 `id` 和断线续传游标：

```text
id: 8
event: run.succeeded
data: {"sequence":8,"kind":"run.succeeded","run_id":"...","turn":1,"payload":{...}}
```

客户端可用 `?after_sequence=5`、`?last_event_id=5` 或 `Last-Event-ID: 5` 只接收 sequence
大于 5 的 Event；Header 优先于 `last_event_id`，再与 `after_sequence` 取较大值。连接空闲时
默认每 15 秒发送一次 SSE comment 心跳，Run 终态事件发出后流自然关闭。Run registry 仍只
存在于服务进程内，重启后原 `run_id` 不再可用，且目前不会自动清理已完成 Run。当前 Server
因此只适合单 Worker；部署到 Kubernetes 多副本或多 Worker 前，必须迁移为共享 RunStore
驱动，不能依赖 sticky session 维持正确性。

Tool、Skill 和 Flow 的完整离线示例见 `examples/quickstart.py`。
Skill Package 的目录、Tool 贡献、Script 执行边界和 snapshot 契约见
[`SKILLS.md`](SKILLS.md)。

模块职责、执行不变量和后续迁移顺序见 [`ARCHITECTURE.md`](ARCHITECTURE.md)，旧实现的
迁入门槛见 [`MIGRATION.md`](MIGRATION.md)。

当前 Provider 契约、OpenAI-compatible Adapter、错误分类和验收命令见
[`PROVIDERS.md`](PROVIDERS.md)。

需要按能力逐步开发和测试时，使用独立下游项目
[`starter/`](starter/README.md)。Starter 当前验收 Step 03：Skill Package；后续仍按单个
能力逐步加入 Flow 等能力，并始终禁止导入 Harness 内部模块。

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
- 简单 SequentialFlow 与显式阶段交接。

SDK Runtime 核心明确不依赖 HTTP Server、Worker、Lease、持久化恢复、Operator API、
插件市场、动态 Tool Search、复杂多 Agent 调度或产品级会话路由。FastAPI Run Server 位于
`base_agent.server` 可选边界，只有安装 `base-agent-next[server]` 并显式导入时才会加载。
