# Event 存储、回放与实时订阅

Event 只有一个事实来源。Harness 不再维护私有 Event 列表，`RunHandle` 也不再复制一份
Event buffer；`EventStore.emit()` 原子分配 Run 内 sequence 并保存 Event，随后才通知
Observer：

```text
Harness / _EventEmitter
          │
          ├─ 1. EventStore.emit() ──► authoritative history
          │                              ├─ RunResult.events
          │                              ├─ RunHandle.events()
          │                              └─ EventStream.subscribe()
          │
          └─ 2. Observer(event) ─────► logs / metrics / UI side consumers
```

Observer 是 fail-open 的旁路消费者，不是可靠存储。EventStore 写入失败会中断执行，不能在
事件尚未成为事实时继续通知外部消费者。

## 默认内存实现

`Agent` 默认创建 `InMemoryEventStore`。它按 `run_id` 隔离 Event、原子分配递增 sequence，
并使用 `asyncio.Condition` 实现无轮询的进程内 replay-then-follow：

```python
from base_agent import Agent
from base_agent.events import InMemoryEventStore

events = InMemoryEventStore()
agent = Agent(definition, model=model, event_store=events)

result = await agent.run("hello")
history = await events.list(result.run_id)
```

`RunHandle.events()` 是异步 API，因为持久实现需要执行数据库 I/O：

```python
handle = agent.start("hello")
current = await handle.events(after_sequence=0)

async for event in handle.stream(after_sequence=last_sequence):
    last_sequence = event.sequence
```

## 扩展契约

- `EventSink.emit()`：原子分配 per-Run sequence 并提交一个 Event；
- `EventStore.list()`：按 sequence 游标读取权威历史；
- `EventStream.subscribe()`：可选的“先回放、再跟随”实时能力。
- `EventSubscription.aclose()`：消费者提前结束时确定性释放数据库游标、Pub/Sub 等资源。

只实现 `EventStore` 的历史存储仍可用于 `Agent.run()`、`RunResult` 和
`RunHandle.events()`；`RunHandle.stream()` 要求同一个对象同时实现 `EventStream`。

## Message Event

Message 不使用第二条 Stream，而是作为同一条有序 Run Event Stream 中的一类事实：

- `message.created`：Run 接受的完整 user prompt，始终产生；
- `message.delta`：assistant 文本增量，只在 Provider streaming 时产生；
- `message.completed`：一个模型回合的完整 assistant 内容，流式和非流式 Provider 都产生。

同一个 assistant 回合的 delta 和 completed 共享 `message_id`。`message.completed` 只表示该
模型消息已经完整；Run 是否接受该输出仍由后续 `run.succeeded` / `run.failed` 决定。消费者
可以实时拼接 delta，但断线回放和最终展示必须以 completed 的完整 `content` 为准。

Prompt 和模型正文可能包含敏感数据。生产 EventStore/SSE Adapter 必须在授权边界内使用，
并在后续 payload policy 中支持保留期、加密、脱敏或禁止正文持久化；不能把 Event 自动写入
普通应用日志。

## HTTP JSON 与 SSE

可选 Server Adapter 直接复用同一份 EventStore，不创建第二套 HTTP Event buffer：

- `GET /v1/runs/{run_id}/events?after_sequence=N` 返回 sequence 大于 N 的历史 JSON；
- `GET /v1/runs/{run_id}/events/stream` 先回放历史，再实时等待新 Event；
- 每个 SSE 帧以 sequence 作为 `id`，以 Event kind 作为 `event`；
- 断线重连支持 `Last-Event-ID` Header，以及 `last_event_id` / `after_sequence` 查询参数；
- 空闲连接默认每 15 秒发送 comment 心跳，心跳不进入 EventStore，也不推进游标；
- `run.succeeded` 或 `run.failed` 发出后订阅自然结束，客户端断开不会取消 Run。

这种分层沿用 mock-manus 的“先写事实记录、再实时分发”和 `Last-Event-ID` 续读思路；不同点
是 `InMemoryEventStore.subscribe()` 已通过同一把 Condition lock 原子完成回放到实时的切换，
因此不需要额外的进程全局 StreamBridge。只实现历史读取的 EventStore 仍可使用 JSON 接口，
SSE 接口会明确返回 `501`。

## PostgreSQL 与 Redis 的演进方向

PostgreSQL 实现应成为 durable EventStore：在事务内按 `(run_id, sequence)` 唯一约束原子
分配 sequence，`list()` 使用 `sequence > cursor ORDER BY sequence`。它可以先用 polling
实现 `EventStream`。

Redis 不作为 Event 事实数据库。Redis Adapter 应包装一个 durable EventStore：

1. 先调用 durable store 的 `emit()` 并取得已提交 Event；
2. 再按 Run channel 发布它的 sequence；
3. 订阅者收到通知后仍按 cursor 从 durable store 对账；
4. Redis 故障时退化为 durable store polling，不能丢失历史。

这种边界吸收了两个已有经验：mock-manus 的“先落事实记录、再实时扇出，以及补历史与实时
订阅之间不能留丢帧窗口”；旧 base-agent 的“PostgreSQL 是事实源、Redis 只提供低延迟通知”。
不会复制 mock-manus 的全局单例 Bridge，也不会把 Redis Stream 同时当事件账本和任务队列。
