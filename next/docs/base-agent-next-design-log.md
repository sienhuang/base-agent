# Base Agent Next × DeepSeek Harness SDK：ContextPolicy、强制事件持久化与 Planner–ReAct Flow 方案 v2.5

> 状态：待评审。本文替代 v2.4 中遗漏 Host-owned Planner–ReAct Flow 的设计。
>
> 更新时间：2026-08-28（Asia/Shanghai）
>
> 飞书协作文档：https://moonton.feishu.cn/docx/BucRdhQJmo29MSxSm78c92znn5e

## 1. 本轮结论

新版 `base-agent` 仍定位为 **DeepSeek Harness 官方 Python SDK 之上的 Python 产品层**，但四项产品语义需要重新划分：

1. **只有一个 `ContextPolicy`。** 它在每次模型请求前决定动态上下文中哪些内容允许进入 AI 的可见范围。History、Compaction、Memory selection 都属于这个决策，不再作为三个平级 Policy。
2. **Compaction 是 `ContextPolicy` 的一种内建实现。** `CompactingContextPolicy` 根据 token 压力把较早历史转换为摘要并保留 recent tail；底层优先复用 DSH 原生 Compaction 事务。
3. **Persistence 不是 Policy。** 每个 Session、Run、Task/Flow 的事件日志必须持久化。用户可以配置存储后端、目录、归档和保留期限，但不能把事实日志关闭。
4. **ReAct 分成两个层次。** 单个 Agent Run 内的模型/Tool/Observation 循环由 DSH Agent Loop 拥有；跨 Planner、Executor、Plan revision 和 Step review 的 `PlannerReActFlow` 仍由 Python Host 拥有，不能因采用 DSH 而消失。

同时确认 DSH 当前没有内建的长期 Memory 子系统：

- DSH 的 Session append-only log 是会话内工作记忆和历史事实源。
- DSH Compaction 是同一 Session 内的历史压缩，不是跨 Session 长期记忆。
- DSH 仓库提供的是第三方 Memory MCP Server 的接入示例，Memory 由外部系统存储，模型通过 Tool 主动读写。
- DSH 没有类似 `MemoryProvider` / `MemoryWriter` / 自动 recall / 自动写回的官方 Runtime API。

因此 Base Agent Next 需要保留自己的 Memory 抽象，但 Memory 只能提供候选事实；**最终哪些 Memory 进入模型，由 `ContextPolicy` 决定。**

## 2. 对 v2.1 的概念修正

| 早期概念 | 当前处理 | 原因 |
| --- | --- | --- |
| `HistoryPolicy` | 删除 | 是否延续历史由 `Conversation` / Session 身份决定；本次请求看哪些历史由 `ContextPolicy` 决定 |
| `CompactionPolicy` | 改为 `CompactingContextPolicy` | Compaction 是上下文选择与转换的一种策略实现 |
| `PersistencePolicy` | 删除 | Session / Run / Flow 事实日志必须持久化，不应成为可关闭策略 |
| Memory 由 DSH 承担 | 否定 | DSH 只有 Session history 和可选第三方 MCP Memory Tool，没有内建长期 Memory API |
| 原型绝不需要 Bridge | 修正 | 不建设通用执行 Bridge；业务自定义 Python `ContextPolicy` 需要一条窄 Context Decision Bridge，或等待 DSH 增加正式 seam |

产品层最终只保留这些核心对象：

- `Tool`
- `Skill`
- `ContextPolicy`
- `Memory`（可选、可插拔的 recall / write 能力）
- `Agent` / `Conversation`
- `Task` / `Flow`（`Sequence` / `PlannerReActFlow`）
- 强制启用的 `EventStore` / `StateStore` 基础设施

## 3. 已核实的 DSH 事实

研究基线：

- DeepSeek Harness 源码提交：`cd5ef8148158c3a752a658978873241fdf8e2bbc`。
- DSH tag：`dsh-v0.1.2-alpha.1`。
- Python 客户端包：`deepseek-harness-sdk`；由 SDK 同版本安装并默认启动的本地 Runtime 载体：`deepseek-harness-runtime-bin`。
- 原型仍要求 SDK 和 Runtime 使用完全相同的版本。

### 3.1 Session 与 History

DSH `dsh-session` 的核心语义是：

- `Session` 是 append-only 的 `SessionEvent` 日志。
- 模型消息通过 `session.deriveMessages()` 从日志的 surface 投影产生，不单独保存另一份 history。
- `user/message`、`assistant/message`、`tool/result` 进入模型可见 surface；chunk、边界和其他 log-only event 不进入消息历史。
- surface replacement 只遮蔽旧节点，不删除原始事件，所以压缩后仍可审计和重放。
- `request/header` 持久化 system prompt、tool schemas 和模型调用配置；模型请求可由 Session 日志重建。

DSH 的重要不变式是：**模型实际看到的请求必须可由 Session 日志重建。** Agent Loop 内建校验会拒绝 `request.messages` 与 `session.deriveMessages()` 不一致的请求。

### 3.2 Context 扩展点

当前 DSH 有以下扩展点：

- `system-prompt/assemble`：可组合 system prompt、runtime context 和 tool schemas。
- `agent/pre-step`：请求派生前唯一的 waterfall，可拒绝 Step，或替换本 Step 新进入的 `UserMessage[]`。
- `agent/request`：可更换 provider/model/call config，但明确不能修改 messages。
- `llm/stream`：只能读取深冻结的 Loop 请求或包裹 stream，不能改写 Loop 请求内容。

这意味着：

- Memory 或运行上下文可以在 `agent/pre-step` 作为有来源的消息进入 Session。
- DSH 原生 Compaction 注册在串行 `agent/pre-step` waterfall 中，并在请求派生前完成必要的 surface replacement。
- 但官方 Python SDK 没有“每个 Step 调用一个 Python 函数，然后任意返回完整 messages”的能力。
- 在 SDK 外拦截并私自裁剪模型请求会破坏 DSH 的日志可重建不变式，方案禁止这样实现。

### 3.3 Compaction

DSH `dsh-compaction-basic` 已提供成熟的会话内压缩：

- 在 `agent/pre-step` 根据路由模型的 context window 和 token 压力自动触发。
- 支持 `thresholdRatio`、`retainRatio` / `retainTokens`、summarization route、summary token 上限和 overflow retry。
- 可以先用 Tool Result Pruner 修剪超大工具结果。
- 用 `compaction/start`、`compaction/summary`、replacement `user/message`、`compaction/end` 形成持久事务。
- 摘要替换较早的平衡 span，recent tail 原样保留；旧事件仍在 append-only log 中。
- context overflow 后只有 surface generation 确实前进才允许重试。

因此 Base Agent 不应再实现第二套 transcript compactor。`CompactingContextPolicy` 应优先编译为这套 DSH 原生能力。

### 3.4 Memory

DSH 当前的“记忆”需要分成四类理解：

| 类型 | DSH 当前能力 | 是否等于长期 Memory |
| --- | --- | --- |
| Session History | append-only Session log + derived surface | 否，只属于一个 Session |
| Compacted History | Session 内摘要 replacement | 否，仍是同一 Session 的历史 |
| Workspace / Skill / Goal Context | 指令、Skill 和目标等作用域上下文 | 否，不是检索式记忆 |
| 第三方 Memory MCP | Memorix、MCP Reference Memory、Engram 等示例 | 是外部 Memory，但只作为模型主动调用的 Tool；不提供上下文可见性控制 |

DSH 的第三方 Memory MCP 配置默认关闭。DSH 只负责发现并注册 `mcp__<server>__<tool>`；下载、数据库、embedding、召回、冲突处理、遗忘和供应商生命周期均由外部系统负责。

更重要的是，MCP Memory 不决定模型可见性：

- 调用前，模型只能看到 Memory Tool schema，看不到 Memory Store 中的记录。
- 是否调用 search/recall 由模型决定，而不是业务 `ContextPolicy` 决定。
- 调用后，返回值成为普通 `tool/result` 并进入 DSH Session surface。
- 该结果在后续 Step / Turn 默认继续可见，直到被 Compaction 或 surface replacement 遮蔽。
- MCP 本身没有 `REQUEST`、`TURN`、`SESSION` 可见范围，也没有在下一次请求前重新审核既有 Tool result 的能力。

结论：**Base Agent 如果需要稳定的自动 recall、租户隔离、候选排序、写回和可见性控制，必须拥有自己的 Memory 能力契约和 ContextView seam。** 第三方 MCP Memory 只能作为显式 Tool 兼容模式，或在模型不可见的 Host 侧被 Adapter 包装成 `Memory`；它不能直接承担 Base Agent 的 ContextPolicy。

#### DSH Memory 的实际执行链

DSH 所谓 Memory 接入，实际复用了完整的普通 Tool 链路：

```text
Cordis memory overlay
  ↓
dsh-mcp-client 启动 stdio Memory Server / 连接 HTTP Server
  ↓ initialize + tools/list
Memory tools 注册为 mcp__<serverName>__<rawToolName>
  ↓
tool schemas 进入每次模型请求
  ↓
模型根据 Tool 描述和 prompt 自己决定是否调用 search / remember
  ↓
DSH 先持久化 assistant tool-call 与 tool/call event
  ↓
ToolRuntime policy pipeline
  ↓ tools/call(rawName, JSON args)
外部 Memory Server 查询或写入自己的数据库
  ↓ MCP result
DSH 映射 text / structured content / image，持久化 tool/result
  ↓
下一 Step 的 session.deriveMessages() 包含该结果
```

读路径：

1. Memory MCP 工具的 schema 对模型可见。
2. 模型决定是否发起 search/read/open 等 Tool call；DSH 不生成 recall query。
3. 外部 Memory Server 执行自己的检索和排序。
4. DSH MCP Client 将结果投影为普通 Tool result。
5. Agent Loop 把 `tool/result` append 到 Session surface；下一 Step 和后续 Turn 会继续看到它。

写路径：

1. 模型决定调用 create/add/remember 等 Tool。
2. DSH 记录调用并转发给外部 Server。
3. 外部 Server 持久化；DSH 不复制其 Memory 数据库。
4. DSH 只在 Session 中保存调用参数和返回结果。

跨 Session 召回：

1. Session A 的写 Tool 把数据存进外部 Memory Server。
2. Session B 没有继承 Session A 的 DSH history。
3. Session B 的模型再次主动调用 search Tool。
4. 外部 Server 在自己的持久存储中找到记录并返回。

这不是 DSH 自动把 Memory 注入新 Session；跨 Session 的连续性完全来自外部 Memory Store，以及模型再次调用 Tool。

外部 Provider 的实际存储也各自独立：

- MCP Reference Memory：本地 JSONL 知识图谱，按实体名称、类型和 observation 做不区分大小写的子字符串搜索；没有 embedding、自动摘要、冲突解决或遗忘策略。
- Memorix：使用自己的数据目录和项目识别，支持其自身的启发式或可选模型配置。
- Engram：使用自己的 `~/.engram` 数据与项目 scope。

DSH 能在 Tool 层提供的控制点只有：按 Agent 限制 Tool schema、在 `tools/pre-execute` / guard 拒绝调用、在 `tools/post-execute` 替换或过滤结果，以及后续用 Compaction / surface replacement 遮蔽历史结果。这些控制点不构成 Memory recall policy，也不构成 REQUEST / TURN 可见性模型。

### 3.5 DSH 当前的模型可见性

DSH 当前决定模型可见内容的事实路径是：

```text
system-prompt assembly
  + visible tool schemas
  + session.deriveMessages() surface
  = one Agent Loop model request
```

它支持 Agent 作用域的 prompt/tool 组合、Session surface 追加和持久 replacement，但没有一等的 `VisibilityScope`：

| 目标作用域 | DSH 当前原生能力 | 限制 |
| --- | --- | --- |
| `REQUEST` | system prompt 可以逐 Step 重新 assembly | 不能对任意历史、Memory 和 Tool result 形成一次性 request view |
| `TURN` | 无通用机制 | 注入消息一旦进入 surface，不会在 Turn 结束时自动失效 |
| `SESSION` | 支持 | append 进入 surface 后持续可见，replacement / Compaction 后才被遮蔽 |
| 跨 Session Memory | 仅外部 MCP Tool | Store 中可检索不等于模型可见；Tool result 仍回到当前 Session surface |

`agent/pre-step` 只能替换本 Step 新进入的消息，不能任意过滤已经存在的 Session surface；`agent/request` 又明确不能改 messages。因此，只依赖现有 SDK / MCP 无法实现“业务在每个 Turn 或请求决定模型能看到什么”。

### 3.6 Persistence

DSH 已有完整的 Session persistence seam：

- `SessionPersistence` 提供 locate/create/append/load/inspect/read/list 等能力。
- JSONL 与 SQLite 都实现同一个持久化约定。
- `session/event` 进入批量写入，`session/flush` 是可等待的 durability barrier。
- 完整 `sdk` Profile 已组合 Session Persistence、Checkpoint、repair 与 Compaction。
- 请求、顶层 Tool 副作用和 Step 边界可以执行 fail-closed durability checkpoint。

但 DSH Session log 只覆盖 DSH Runtime 内的事实。Base Agent 的 Run 身份、ContextPolicy 决策、Memory recall/write、Task/Flow handoff 等 Host 事件仍需 Base Agent 自己的强制 EventStore。

### 3.7 官方证据

- Python SDK：https://github.com/deepseek-ai/deepseek-harness/tree/master/python/sdk
- SDK Runtime：https://github.com/deepseek-ai/deepseek-harness/tree/master/python/sdk-runtime
- Session：https://github.com/deepseek-ai/deepseek-harness/tree/master/packages/core/session
- Agent Loop：https://github.com/deepseek-ai/deepseek-harness/tree/master/packages/core/agent-loop
- System Prompt：https://github.com/deepseek-ai/deepseek-harness/tree/master/packages/core/system-prompt
- Compaction：https://github.com/deepseek-ai/deepseek-harness/tree/master/packages/compaction/compaction-basic
- Persistence：https://github.com/deepseek-ai/deepseek-harness/tree/master/packages/session
- 第三方 Memory MCP 指南：https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/user/guide/mcp-memory.md

## 4. 产品 API 方向

接口名称仍是方案草图，不代表冻结：

```python
from base_agent_next import (
    Agent,
    BaseAgentApp,
    CompactingContextPolicy,
    LocalState,
    PostgresMemory,
    Sequence,
    Skill,
    Task,
    tool,
)

@tool
async def search_order(order_id: str) -> dict:
    """查询订单。"""
    return {"order_id": order_id, "status": "paid"}

# State 是必需基础设施；LocalState 可提供默认目录，但不存在 off/ephemeral policy。
app = BaseAgentApp(
    state=LocalState(".base-agent/state"),
    memory=PostgresMemory(dsn="postgresql://..."),
)

analyst = Agent(
    app=app,
    name="order-analyst",
    instructions="分析订单问题并给出处理建议。",
    tools=[search_order],
    skills=[Skill("skills/order-analysis")],
    context_policy=CompactingContextPolicy(
        threshold_ratio=0.8,
        retain_ratio=0.16,
    ),
)

# 独立 Run 仍会持久化，只是不与其他 Run 共享 Session history。
result = await analyst.run("分析订单 A-100")

# 显式 Conversation 复用同一个持久 Session。
conversation = analyst.conversation("tenant-demo/order-A-100")
result = await conversation.run("继续分析刚才的问题")

flow = Sequence(
    Task("analyze", analyst),
    Task("review", reviewer, input_from="analyze.output"),
)
flow_result = await flow.run("处理订单 A-100")
```

自定义业务 Policy 的目标接口：

```python
class TenantContextPolicy:
    async def decide(self, ctx: ContextPolicyContext) -> ContextDecision:
        memories = ()
        if ctx.current_input.may_depend_on_history:
            memories = await ctx.memory.recall(
                text=ctx.current_input.text,
                namespace=ctx.tenant_id,
                limit=8,
            )

        return ContextDecision(
            history=ctx.history.recent_balanced(max_tokens=12_000),
            memories=[m for m in memories if m.score >= 0.75],
            context_blocks=ctx.context_blocks.trusted_only(),
        )
```

这里的关键语义是：

- Policy 自己决定是否调用 Memory，而不是 Runtime 每次无条件 recall。
- Memory search 返回候选项，不会自动出现在模型请求中。
- Policy 返回选择结果；Runtime 校验预算、来源、角色和 Tool call/result 配对后才能提交。
- Context 决策及最终模型可见投影必须持久化并可重建。

## 5. ContextPolicy 设计

### 5.1 Policy 的责任边界

`ContextPolicy` 只决定动态上下文：

- 当前请求、当前 Turn 或后续 Session 请求中，哪些历史进入模型。
- 旧历史是否需要 compact、window、prune 或转换为摘要。
- 是否查询长期 Memory，以及选择哪些 Memory candidates。
- 哪些 Workspace、Run、业务 Context blocks 进入请求。
- 如何在 context window 预算内分配 history、memory 和其他上下文。

以下内容不允许普通业务 Policy 任意删除或提权：

- Base Agent / DSH 的安全 system instructions。
- 当前用户输入和明确的 steering。
- Tool schemas 与 Tool 权限结果。
- Tool call/result 的协议配对。
- 运行时必须注入的风险信息，例如未知副作用 Tool outcome。

因此更准确的请求模型是：

```text
Model Request
  = Protected Envelope
      (安全 system instructions + current input + tool schemas + protocol facts)
  + ContextPolicy Decision
      (history projection + memory selection + runtime/workspace context)
```

必须区分两个完全不同的状态：

```text
Memory availability
  = 配置的 Memory 实现能够在给定 tenant/namespace 下召回某条记录

Model visibility
  = 某条记录被 ContextPolicy 选中，并进入某一个已持久化、可重建的 ContextView
```

记录能被 Memory 实现召回、出现在 recall result、甚至曾在过去一个 Turn 中可见，都不代表它自动对当前请求可见。

### 5.2 VisibilityScope

`ContextDecision` 必须为每个注入块声明作用域：

| Scope | 语义 | 提交方式 |
| --- | --- | --- |
| `REQUEST` | 只对当前一次模型调用可见 | 写入本次 `request/context-view`，不追加为长期 Session surface |
| `TURN` | 对当前 Turn 的所有后续 Step 可见，Turn 结束失效 | 每个 Step 的 request view 引用同一 Turn-scoped block |
| `SESSION` | 对当前 Session 后续请求持续可见，直到显式 revoke / replace / compact | 进入 DSH Session surface |

长期 Memory 不是第四种 visibility scope。它只是跨 Session 可检索的存储层；每次进入模型前仍必须产生新的 `ContextDecision`。

### 5.3 输入与输出

建议的 `ContextPolicyContext` 至少包含：

- `run_id`、`session_id`、`turn`、`step`、tenant / namespace。
- 当前 user input 与来源。
- DSH Session 的不可变事件引用、当前 surface 和已派生 history。
- 当前 system/runtime context blocks 的来源、trust level 和 token estimate。
- 可用模型、context window、已占用预算和 Tool schema token estimate。
- 受边界保护、只暴露 recall 的 `MemoryAccess`。
- 上一次 compaction/context decision 的位置和摘要元数据。
- cancellation / deadline。

建议的 `ContextDecision` 至少包含：

- 被选中的 history event seq / balanced spans。
- 需要产生的 summary 或 replacement action。
- 被选中的 Memory record id 与注入格式。
- 被选中的 context block id。
- 每个 selected block 的 `REQUEST` / `TURN` / `SESSION` visibility scope。
- 预算结果。
- `reason_code`、Policy 名称和版本。
- 用于审计与重放的 decision digest。

### 5.4 内建实现

首版建议提供：

- `KeepAllContextPolicy`：保留 DSH 当前完整 surface，不做长期 Memory recall。
- `CompactingContextPolicy`：复用 DSH `compaction-basic`，按压力摘要旧历史并保留 recent tail。
- `RecentWindowContextPolicy`：保留满足协议平衡的近期历史；不能把 Tool call 与 result 拆开。
- `MemoryAugmentedContextPolicy`：在另一个 Policy 上增加按需 recall 和 Memory selection。
- `CompositeContextPolicy`：按固定顺序组合预算、history、memory 和业务 block 选择器。

对外只有一个 `ContextPolicy` Protocol；上面都是实现或组合器，不再产生平级的 History / Compaction / Memory Policy 家族。

### 5.5 Context 决策必须满足的不变式

1. **事实日志只追加。** Policy 不删除原始 Session / Run 事件。
2. **模型可见即可重建。** 不能只在网络请求中临时改 messages。
3. **当前输入不可静默丢失。** 拒绝输入必须形成明确的 blocked/denied 结果和事件。
4. **协议配对完整。** Tool call、Tool result 和多模态引用必须保持合法。
5. **来源与权限不升级。** Memory、Tool result、Run context 不能伪装成 system instruction。
6. **预算确定。** 同一候选 revision、Policy 版本和模型容量必须产生可解释的确定性选择；涉及摘要模型时记录模型调用事实。
7. **并发安全。** Policy 基于一个 Session revision 决策；提交前 revision 改变则重算或失败。
8. **失败关闭。** required Policy/Memory 失败时不发送未经 Policy 审核的完整上下文。
9. **作用域到期即不可见。** REQUEST / TURN block 到期后，即使原始事实仍被持久化，也不得自动进入下一次 ContextView。

## 6. ContextPolicy 与 DSH 的集成

### 6.1 可以直接复用 DSH 的部分

- `KeepAllContextPolicy`：直接使用 DSH Session surface。
- `CompactingContextPolicy`：编译成 `dsh-compaction-basic` 配置和 Tool Result Pruner。
- Workspace / runtime context：使用 DSH system-prompt 和 source-tagged context message。
- `SESSION` scope 的 Memory / Context 注入：作为有明确 plugin source 的 context snapshot 写入 Session。
- 事件：复用 DSH compaction、request、turn、step、tool 和 session 事件。

现有 DSH 不能直接实现 `REQUEST` / `TURN` scope 的任意 ContextView，也不能在每次请求前从已有 surface 中选择性隐藏 history 或 Tool result。

### 6.2 当前 SDK 缺失的能力

任意业务 Python `ContextPolicy` 要在每个模型 Step 读取候选上下文并决定最终可见投影，当前官方 SDK 不能直接完成：

- SDK 没有 per-step Python callback。
- `agent/request` 不能改 messages。
- `llm/stream` 收到的是深冻结请求。
- DSH invariant 要求请求 messages 与 Session durable derivation 一致。

因此这里不是“可能需要扩展”，而是一个明确边界：**只要 Base Agent 承诺业务可按请求或 Turn 决定可见内容，就必须增加 request-context derivation seam。** 如果不增加这个 seam，产品只能提供 DSH 原生的 Session 可见性和 Compaction，不能把 API 命名为通用 `ContextPolicy`。

### 6.3 推荐的窄 Context Decision Bridge

为了满足产品目标，A0 需要验证一条专用扩展，而不是建设通用执行 Bridge：

```text
DSH agent/pre-step / proposed agent/context seam
  │ candidate revision + history refs + budgets + context refs
  ▼
Base Agent Context Gateway（loopback）
  │ invoke Python ContextPolicy / MemoryAccess
  ▼
ContextDecision + VisibilityScope
  │ validate + append request/context-view + optional session surface mutation
  ▼
DSH derives and dispatches the exact durable model request
```

约束：

- Bridge 只承载 Context candidate 和 decision，不承载模型流、Tool 调用或 Agent 控制。
- 大正文优先用 event seq、Memory id 和 content digest 引用；只传 Policy 真正需要的候选内容。
- Context plugin 在 DSH 内校验 decision，并负责把 exact `ContextView` 写入 durable Session channel。
- `REQUEST` / `TURN` block 通过请求级 projection 生效，不得为了方便永久 append 到 Session surface。
- `SESSION` block 才允许进入普通 surface；revoke / replacement 同样形成新事件，不删除原始事实。
- Policy timeout、异常、revision conflict 和非法角色转换都必须 fail-closed。
- 每个 decision 有 `policy_name`、`policy_version`、`input_revision`、`selected_refs` 和 digest。
- 用户敏感内容不复制到普通日志；精确可见正文只进入受保护的 Session persistence。

优先实现顺序：

1. 用现有 `agent/pre-step` + surface operation 验证 `SESSION` scope、原生 Compaction 和 Memory snapshot。
2. 向 DSH 上游增加一个位于 request derivation 前、保持日志可重建的 `agent/context` / `request/context-view` seam，验证 `REQUEST` / `TURN` scope 和已有 surface 的选择性投影。
3. 扩展 request reconstruction invariant，使实际 messages 必须等于当次持久化 ContextView，而不是无条件等于完整 `session.deriveMessages()`。
4. 原型期间如上游尚未合入，只允许维护这一处精确 pin 的最小补丁；不 fork 整个 Agent Loop，不扩展成通用 Bridge。

A0 必须给出“完全通过官方插件组合”或“需要最小上游 seam”的可运行证据，不能在文档里假定问题已经解决。

## 7. Memory 方案

### 7.1 `Memory` 是能力端口，不是 Store

公共抽象不命名为 `MemoryStore`，因为 Base Agent 不要求自己持有数据库。它只要求一个可插拔实现提供 recall 和 write 行为：

```python
class Memory(Protocol):
    async def recall(
        self,
        query: RecallQuery,
    ) -> tuple[MemoryCandidate, ...]: ...

    async def write(
        self,
        command: MemoryWrite,
    ) -> MemoryRef: ...
```

语义：

- `recall`：按 tenant / namespace / subject、query、filters 和 limit 返回有界候选；候选有稳定 ref、score、content、metadata 和来源。
- `write`：把经过业务确认的 Memory 写入实现方，携带 tenant scope、source event ref、幂等键和 metadata，返回稳定 `MemoryRef`。
- delete / forget、update、feedback 和 batch 是后续可选 capability，不进入最小 Protocol。
- `Memory` 可以不配置；未配置时 `ContextPolicyContext.memory` 明确为空能力，而不是隐式使用 DSH MCP Memory。

`Memory` 只定义行为，不定义物理持久化、索引算法、embedding、冲突解决和数据迁移。

### 7.2 可插拔实现

| 实现 | 数据归属 | 用途 |
| --- | --- | --- |
| `InMemoryMemory` | Base Agent 进程内 | 单元测试、本地开发；重启丢失，不用于生产 |
| `PostgresMemory` | PostgreSQL / 可选 pgvector | Base Agent 自托管的持久实现，支持结构化过滤和可替换检索策略 |
| `ApiMemory` | 外部 HTTP / gRPC 服务 | 外部服务拥有存储和召回；Base Agent 只调用 API |
| `McpMemory` | 外部 MCP Memory Server | Host-side MCP Client Adapter；结果先回到 ContextPolicy，不直接作为模型 Tool |

推荐用户接口：

```python
# 测试 / 本地
memory = InMemoryMemory()

# 自托管生产
memory = PostgresMemory(dsn="postgresql://...")

# 已有企业 Memory 服务
memory = ApiMemory(client=my_memory_client)

agent = Agent(
    ...,
    memory=memory,
    context_policy=policy,
)
```

自定义实现只需满足 `Memory` Protocol。`ApiMemory` 和 `McpMemory` 说明公共抽象不拥有 Store；远端甚至可以是完全不同的数据模型。

### 7.3 Runtime 包装层

Base Agent 在用户实现外增加统一 `MemoryRuntime`，但它不是另一套存储：

- 强制 tenant / namespace scope 和调用权限。
- 给 write 生成并校验幂等键。
- 施加 timeout、cancellation、limit 和 failure mode。
- 归一化 candidate score / ref / source。
- 持久化 `memory.recall.*`、`memory.write.*` 事件。
- 默认不把 Memory content 复制进普通 Run event，只记录 ref、score、scope 和 digest。

`ContextPolicy` 只获得受限的 `MemoryAccess.recall()`，避免在上下文选择阶段产生 write 副作用。write 通过显式应用 API、`remember` Tool 或受控 extractor 进入同一个 `MemoryRuntime`。

### 7.4 Recall 路径

Memory 不在每个 Run 开始时无条件自动注入：

1. `ContextPolicy` 收到当前 input、history 和 `MemoryAccess`。
2. Policy 判断是否需要 recall。
3. `MemoryRuntime` 调用配置的 `Memory.recall()`。
4. `Memory` 实现可以查进程内索引、PostgreSQL，也可以调用远端 API / MCP。
5. Policy 选择零个或多个 candidates。
6. Policy 为候选项指定 `REQUEST` / `TURN` / `SESSION` visibility scope。
7. Context Bridge 把 selected candidates 写入当次可重建 ContextView；只有 `SESSION` scope 才进入长期 surface。
8. `memory.recall.*`、`memory.selected` 和 `context.decision.*` 事件持久化。

这同时保证“召回到了”不等于“模型看到了”。

### 7.5 Write 路径

Memory write 必须显式触发，首版提供两种入口：

- 明确的 `remember` Tool 或业务应用代码。
- Run 完成后的受控 Memory extractor，经 verifier 后写入。

两条入口都调用 `MemoryRuntime.write()`，再委托给配置的 `Memory.write()`。默认不把整段对话自动写入 Memory；每次写入都记录来源 Session/Event、tenant scope、实现名称/版本和幂等键。

### 7.6 与模型可见 MCP Memory Tool 的关系

- 直接把 DSH MCP Memory Tool 暴露给模型，是**模型驱动兼容模式**，不是 Base Agent `Memory` 的默认实现路径。
- 直接模式下，模型决定何时搜索；Tool 返回内容成为 Session `tool/result`，默认持续对后续请求可见。
- 如果需要业务控制可见性，应使用 Host-side `McpMemory` 实现：由 `MemoryRuntime` 调用 MCP Server，候选先回到 ContextPolicy。
- 同一个原始 search Tool 默认不再同时暴露给模型，否则模型可以绕过 ContextPolicy。
- 第三方实现的 embedding、数据迁移、冲突和遗忘仍由实现方负责。

## 8. 强制事件持久化

### 8.1 不再存在 PersistencePolicy

公开 API 中删除：

- `PersistencePolicy.ephemeral()`
- `PersistencePolicy.local()`
- 任意关闭 Session / Run 事实日志的开关

改为基础设施配置：

- `StateStore`：状态根、事务、锁和版本。
- `EventStore`：Run / Task / Flow append-only event stream。
- `DshSessionStore`：DSH JSONL 或 SQLite Session persistence。
- `RetentionConfig`：归档、TTL、配额和删除流程；它管理生命周期，不决定“是否记录”。

本地开发可以有默认 `LocalState`；测试可以显式使用 `InMemoryEventStore`，但生产配置不得落入内存实现。

### 8.2 三层事实流

| 事件流 | 权威内容 | 物理实现 |
| --- | --- | --- |
| Session stream | 模型可见消息、request header、Turn/Step、Tool、Compaction、repair | DSH Session Persistence |
| Run stream | Run 生命周期、Agent fingerprint、ContextPolicy、Memory、SDK 映射、终态 | Base Agent EventStore |
| Flow stream | Task 开始/完成、handoff、输入输出引用、分支与 Flow 终态 | Base Agent EventStore |

不盲目复制大正文：

- DSH Session Store 保存精确对话事实和正文。
- Base Agent Run event 使用 `session_id + session_seq` 引用对应 DSH 事实，并记录归一化元数据。
- 配置的 Memory 实现拥有 Memory 正文；Run event 保存 record ref、score、scope 和 digest。
- Flow event 保存 Run / artifact / output reference；敏感正文放受权限控制的对象存储。

### 8.3 最小事件目录

Base Agent 至少持久化：

- `run.created`、`run.started`、`run.completed`、`run.failed`、`run.cancelled`。
- `runtime.started`、`runtime.version_verified`、`runtime.stopped`、`runtime.crashed`。
- `context.decision.started`、`context.decision.committed`、`context.decision.failed`。
- `memory.recall.started`、`memory.recall.completed`、`memory.recall.failed`、`memory.selected`、`memory.write.started`、`memory.write.completed`、`memory.write.failed`。
- `session.bound`、`session.resumed`、`session.forked`、`session.checkpointed`。
- DSH Tool / Compaction / Turn / Step 事件的归一化引用。
- `task.started`、`task.completed`、`task.failed`。
- `planner.started`、`planner.completed`、`planner.failed`、`plan.created`、`plan.revised`、`plan.completed`、`plan.failed`。
- `step.started`、`step.completed`、`step.failed`、`flow.completed`、`flow.failed`、`flow.cancelled`。

### 8.4 Durability 语义

- 创建 Run 后必须先持久化 `run.created`，再启动 DSH 副作用。
- Context decision 必须在模型请求前提交；exact visible projection 必须已进入 DSH durable channel。
- 顶层有副作用 Tool 执行前必须完成 checkpoint。
- Tool 结果在下一 Step 使用前必须持久化。
- Flow handoff 前必须持久化前一个 Task 终态和输出引用。
- Planner decision 必须先通过 Host schema 与 revision 校验并持久化，再创建、修订或完成 Plan；Executor outcome 必须先持久化，再进入 Planner review。
- 任何 durability barrier 失败都停止后续副作用，不能退化为 warning。
- 崩溃恢复只根据事实日志判断；未知副作用结果标记 unknown，不自动重试。

Task/Flow 在 A1 就必须记录完整事件。A2 可以再增加 durable resume，但“尚未支持恢复”不能成为“不记录事件”的理由。

## 9. 新目标架构

```text
用户应用
  │ Tool / Skill / ContextPolicy / Task / Flow
  ▼
Base Agent Next（Python）
  ├─ AgentSpec / Conversation / identity
  ├─ ContextPolicy Runtime
  ├─ Memory Protocol / MemoryRuntime
  ├─ Python Tool Gateway
  ├─ mandatory EventStore / StateStore
  ├─ SDK Runtime Adapter / event normalization
  └─ Host-owned Task / Flow orchestration
       ├─ Sequence
       └─ PlannerReActFlow
  │
  ├──── narrow Context Decision Gateway
  │
  │ official deepseek-harness-sdk
  ▼
DeepSeek Harness Runtime
  ├─ sdk Profile + Base Agent Patch
  ├─ Session / Turn / Step / Agent Loop
  ├─ MCP Client / Skill / Prompt
  ├─ Context Decision companion plugin
  ├─ History surface / native Compaction
  ├─ mandatory Session Persistence / checkpoint
  └─ LLM adapters
```

状态归属：

| 状态 | 权威来源 |
| --- | --- |
| Agent 声明、Policy/Tool/Skill 版本与 fingerprint | Base Agent Next |
| Agent Loop、Turn/Step、模型与 Tool 调度 | DSH |
| 原始 Session event log 与 exact model-visible facts | DSH Session Persistence |
| ContextPolicy 代码和业务决策 | Base Agent；决定结果同步为 DSH durable context facts |
| 长期 Memory 正文与索引 | 配置的 Memory 实现：进程内、PostgreSQL 或外部 API/MCP Provider |
| Memory recall/selection/write 事件 | Base Agent EventStore |
| Run / Task / Flow 生命周期 | Base Agent EventStore |
| Plan identity/revision、Step 状态与 Planner/Executor child Run 关联 | Base Agent EventStore / StateStore |
| conversation id → DSH home/session/config fingerprint | Base Agent StateStore |

## 10. Runtime 与 Conversation 生命周期

- 一个不可变 Agent 配置指纹对应一个 `DeepSeekHarness` Runtime 实例。
- 不依赖 `DeepSeekHarness()` 隐式选择的 bundled 默认配置；基于精确 pin 的完整 `sdk` Profile 生成并显式传入 Base Agent 专用 composition/patch，保留 SDK JSON-RPC、Session Persistence、Checkpoint、Compaction、Skill 和 Base Agent companion plugin，并限制未声明的模型可见 Tool。
- 每次独立 `Agent.run()` 创建新 Session；该 Session 仍必须持久化，只是不被后续独立 Run 自动复用。
- `Conversation` 通过稳定 id 复用同一个 DSH Session。
- 打开旧 Agent fingerprint 的 Conversation 默认拒绝；用户显式选择 continue、fork 或 migration。
- Base Agent StateStore 保存 conversation/run/session 映射，但不复制 Session 对话正文。
- SDK 同步 API 由受控工作线程封装为 async。
- 同一 Runtime 原型阶段串行根 Run；并行必须使用隔离 Runtime lease。
- DSH Home 明确属于应用状态目录，不再有会自动删除事实日志的 ephemeral 模式。
- 测试隔离通过临时 StateStore 完成，测试结束时由测试夹具清理。
- 单个 Session 必须保持单写者；锁和 owner 信息进入 StateStore。

## 11. Tool 与 Skill

Tool 和 Skill 主线保持 v2.1 设计：

- Python sync/async callable 通过内部 loopback MCP Gateway 暴露给 DSH。
- schema 从类型标注和 docstring 生成，参数/结果遵循 lossless JSON。
- Gateway 只监听 loopback，使用随机 bearer token，凭据不落事件日志。
- DSH MCP Client 负责把 Gateway Tool 注册到原生 Agent Loop。
- Skill 直接使用标准 `SKILL.md` 和 DSH Skill Filesystem，不建立平行 Skill Runtime。
- Base Agent 负责路径、frontmatter、名称和资源引用的开发期校验。
- Tool schemas 和权限属于 protected envelope，不由普通 ContextPolicy 任意改写。

首版仍不兼容旧版 `ToolContext`、Artifact、Approval/WAITING 和 operation id 的完整语义；这些能力分别做 ADR。A1 只提供 `PlannerDecision` / `StepOutcome` 所需的最小结构化结果校验，不在此阶段恢复通用 structured result 产品面。

## 12. Task / Flow 编排

Task / Flow 继续由 Python Host 拥有：

- `Task`：一次命名的 `Agent.run()`。
- `Sequence`：顺序执行并显式 handoff。
- `PlannerReActFlow`：固定 Planner 与 Executor、由 Host 持有 Plan identity/revision 的动态计划执行与逐步 review。
- `FlowResult`：引用每个 Task 的 Run、输出和错误。
- Host verifier：可选纯 Python 校验和有界重试。

必须区分两层 ReAct：

```text
单个 Executor Run 内
  model → tool call → tool result → next Step
  由 DSH Agent Loop 拥有

多个 Run 之上
  PLANNING
     ↓
  EXECUTING(one Plan Step / one Executor Run)
     ↓
  REVIEWING ── continue/revise ──> EXECUTING
     └────── complete/fail ──────> terminal
  由 Base Agent Python Host 拥有
```

`PlannerReActFlow` 的首版不变式：

1. Planner create/review 必须返回 Host 校验的 `PlannerDecision`，Executor 必须返回 Host 校验的 `StepOutcome`；这是 A1 的最小结构化结果能力。
2. 每次 Planner create/review 和每个 Executor Step 都是独立的普通 `Agent.run()` / DSH Session；Flow 不接管 DSH 内部 ReAct Loop。
3. `flow_run_id`、`plan_id`、`step_id`、revision、attempt 和 child `run_id` 由 Host 生成并持久化，模型不能直接修改状态存储。
4. Planner review 只能保留或替换尚未执行的 Plan 后缀；已经执行的终态前缀不可改写。
5. Planner 与 Executor 可以分别配置模型、ContextPolicy、Memory namespace、Tool/Skill 和预算；Plan/Step/Outcome 通过显式、有限、可验证的 handoff 传递，不作为 Message 或 Memory 隐式共享。
6. Flow 使用独立的 `max_steps`、`max_revisions` 和 `max_child_runs`，不能只依赖单个 DSH Run 的 Turn/Tool 限额阻止跨 Run 循环。
7. Flow event 只保存编排事实和 child Run 引用；详细模型、Tool 和 Session 事实保留在对应 child Run / DSH Session stream。

A1 交付 `Sequence` 与最小 `PlannerReActFlow` vertical slice，并持久化全部 Task / Flow / Plan / Step 事件；A2 再增加：

- durable Task/Sequence/PlannerReActFlow resume。
- `Parallel` 和隔离 Runtime lease。
- DAG / branch / join。
- 已完成 Task 不重放。
- 已完成 Plan Step 不重放；恢复时校验 Plan revision、Flow 定义和 child Run 关联。
- 未知副作用状态不自动越过。

首版不建设 Queue、Worker、Lease/Fencing 和跨进程 exactly-once Control Plane。

## 13. 推荐代码结构

```text
src/base_agent_next/
  __init__.py
  app.py                    # BaseAgentApp / mandatory state wiring
  agent.py                  # Agent / Conversation / RunResult
  tools.py                  # @tool / ToolSpec / schema
  skills.py                 # Skill 路径与静态校验
  context/
    protocol.py             # ContextPolicy
    models.py               # candidates / decision / trace
    builtin.py              # keep-all / compact / window / memory augmented
    runtime.py              # invoke, validate, revision and budget checks
  memory/
    protocol.py             # Memory recall / write capability
    models.py               # RecallQuery / Candidate / Write / Ref
    runtime.py              # scope / idempotency / events / failure handling
    in_memory.py            # InMemoryMemory
    postgres.py             # PostgresMemory
    api.py                  # ApiMemory / custom client adapter
    mcp.py                  # Host-side McpMemory adapter
  events/
    models.py               # Run / Flow event vocabulary
    store.py                # mandatory EventStore
    journal.py              # DSH seq references and query projection
  state.py                  # LocalState / locks / identity catalog / retention config
  orchestration/
    task.py
    sequence.py
    planning.py             # PlannerDecision / StepOutcome / Plan revision
    planner_react.py        # Host-owned create / execute / review state machine
  _dsh/
    runtime.py              # official SDK adapter
    profile.py              # DSH Home / Profile Patch
    tool_gateway.py         # internal MCP Server
    context_gateway.py      # narrow Python ContextPolicy endpoint
    event_adapter.py        # SDK notification → durable Run references
    session_catalog.py      # conversation/run/session mapping
    version.py              # exact SDK/runtime compatibility
  dsh_plugin/
    context-decision/       # narrow Cordis companion plugin, preferably upstreamed
```

原型仍使用独立 `base_agent_next` namespace，验证后再决定替换现有包。

## 14. 原型阶段与验收门

### A0：DSH 能力与 Context seam Spike

必须证明：

- 同 tag SDK + Runtime 能启动、握手、关闭和恢复 Session。
- Python Tool 经 MCP Gateway 调用；显式 Skill 被发现并加载。
- DSH Session JSONL/SQLite 和 Base Agent EventStore 同时强制启用。
- 独立 Run 也有 durable Session / Run log；Conversation 重启后继续。
- `KeepAllContextPolicy` 与 `CompactingContextPolicy` 能直接复用 DSH。
- Compaction summary、replacement、recent tail 和 overflow recovery 可观察且持久化。
- 一个业务 Python `ContextPolicy` 能在每个 Step 基于 Session revision 选择 history / memory，并分别验证 `REQUEST`、`TURN`、`SESSION` visibility scope。
- 前一个 Turn 可见但当前 Policy 未选择的 Memory / Tool result，不得出现在当前模型请求中。
- 实际模型请求必须与当次 durable `request/context-view` 完全一致。
- ContextPolicy timeout、非法 decision、revision conflict 和 Memory failure 能 fail-closed。
- DSH 原生 MCP Memory Tool、Host-side MCP Memory Adapter 与 Base Agent ContextPolicy recall 的语义差异有 contract test。
- 请求前、顶层 Tool 前、Step 与 Flow handoff 的 durability barrier 可通过故障注入验证。

退出条件：

- 当前 DSH 已确认缺少通用 request-context derivation seam；A0 必须形成最小上游改动 ADR 和可运行补丁。
- 补丁只允许增加 ContextView 的持久化、派生与 invariant，不得扩展成通用执行 Bridge 或 Agent Loop fork。
- 如果无法保持请求可重建与 fail-closed，则停止 A1，不得用 SDK 外层临时 messages rewrite 伪装完成。

### A1：开发者 API Vertical Slice

交付：

- `BaseAgentApp`、mandatory LocalState/EventStore、`Agent.run()` 和 `Conversation`。
- `@tool`、`Skill`、`ContextPolicy` Protocol 和内建 Policy。
- `Memory` Protocol、`MemoryRuntime`、`InMemoryMemory`、`PostgresMemory` 参考实现和 `ApiMemory` Adapter contract。
- Context Decision Gateway / DSH companion plugin。
- Context / Memory / Session / Run / Task / Flow 事件完整持久化。
- State catalog、fingerprint、单写者和重启继续。
- 两个 Agent 的 `Sequence`，以及 Flow event log。
- `PlannerDecision` / `StepOutcome` 最小结构化结果校验，以及 Planner create → Executor Step → Planner review 的 `PlannerReActFlow` vertical slice。
- Planner/Executor child Run 关联、Plan revision、不可变已执行前缀和独立跨 Run 循环限额。
- Fake model / contract tests，不依赖真实模型完成主测试。

### A2：稳定性与恢复

- SQLite/PostgreSQL EventStore 与生产迁移机制。
- Runtime 崩溃诊断、孤儿进程清理和事件脱敏。
- durable Task/Sequence/PlannerReActFlow resume。
- Retention / archive / delete workflow。
- Memory extractor、幂等写入和 tombstone。
- ContextPolicy 性能、cache、deadline 和降级测试。
- 对应公开 wheel 可用时替换构建来源。

### A3：旧能力按需评估

- 超出 `PlannerDecision` / `StepOutcome` 内建契约的通用 structured result 产品面。
- approval / human input。
- Artifact。
- durable WAITING / resume。
- Queue / Worker / 多进程恢复。

优先级仍是：官方 SDK → Profile/Patch → 官方 Plugin → 上游贡献 → 最小伴生扩展。不得为了旧兼容恢复完整 Control Plane。

## 15. A1 验收矩阵

| 场景 | 通过标准 |
| --- | --- |
| 独立 Run | 新 Session、Run event stream 均持久化，但不自动共享历史 |
| Conversation | 显式复用历史，重启可恢复，fingerprint 冲突拒绝静默继续 |
| Keep All | 模型看到完整合法 surface，request 可由日志重建 |
| Compaction | 自动触发、保留 recent tail、summary/replacement 持久化 |
| Custom ContextPolicy | 业务代码逐 Step 决定 history/memory/context，非法决策 fail-closed |
| Request visibility | `REQUEST` block 只出现一次，下一 Step 不再可见 |
| Turn visibility | `TURN` block 在当前 Turn 的 Step 间可见，下一 Turn 不再可见 |
| Session visibility | `SESSION` block 持续可见，直到显式 revoke/replace/compact |
| Memory optional | Policy 不搜索时零 recall；搜索后只有 selected candidates 进入模型 |
| Memory isolation | tenant/namespace 不串数据，事件不泄露 Memory 正文 |
| Memory contract | 同一 recall/write contract suite 通过 `InMemoryMemory`、`PostgresMemory` 和 `ApiMemory` fake adapter |
| Memory durability | `PostgresMemory` 重启后可 recall 已确认 write；`InMemoryMemory` 明确只用于测试/开发 |
| Persistence | 不存在 off；请求、Tool、Step、Flow handoff barrier 失败即停止 |
| Event replay | Session、Run、Flow 事实能还原终态和每次 Context decision |
| Python Tool | sync/async Tool 可发现、调用、返回；错误有稳定映射 |
| Skill | 显式 Skill 可发现；未声明目录不进入 catalog |
| Sequence | 两个 Agent 明确 handoff，Task 事件持久化，失败不静默跳过 |
| Flow structured result | `PlannerDecision` / `StepOutcome` 解析、schema 校验或 revision 校验失败时 fail-closed，不提交 Plan/Step 状态 |
| Planner–ReAct | Planner create、单 Step Executor ReAct Run、Planner review 可完成闭环；模型不能生成或改写 Host identity |
| Plan revision | Review 只修改未执行后缀，已执行终态前缀不变；`max_steps` / `max_revisions` / `max_child_runs` 生效 |
| Flow event replay | `plan.*`、`planner.*`、`step.*`、`flow.*` 与 child Run 引用可还原 A1 Flow 终态和 Plan revision |
| Version | SDK/runtime/plugin 不同版本时启动前失败 |
| Security | Context 不提权；Tool Gateway loopback；凭据不进事件日志 |

## 16. 待评审决策

### D-001：SDK-first

- 建议：确认。
- 含义：DSH 继续拥有 Agent Loop；Base Agent 不重写模型与 Tool 执行内核。

### D-002：完整 `sdk` Profile + 生成 Patch

- 建议：确认。
- 含义：不依赖 Python SDK 的隐式 bundled 默认配置；显式生成并传入基于 pinned 完整 `sdk` Profile 的 Base Agent composition/patch，保留 Session、Compaction、Persistence、Checkpoint 和 SDK JSON-RPC，并限制未声明的模型可见 Tool。

### D-003：Python Tool 经内部 MCP Gateway

- 建议：确认。

### D-004：Skill 采用 DSH 原生机制

- 建议：确认。

### D-005：ContextPolicy 是唯一动态上下文策略

- 建议：确认。
- 含义：删除 `HistoryPolicy` / `CompactionPolicy`；Compaction、window、Memory selection 都是 ContextPolicy 实现或组合。

### D-006：Memory 是可插拔 recall / write 能力，不是 Store

- 建议：确认。
- 含义：公共 `Memory` Protocol 只约定 `recall()` / `write()`；实现可以是 `InMemoryMemory`、`PostgresMemory`、`ApiMemory` 或 Host-side `McpMemory`。Memory 候选只有经 ContextPolicy 选中并进入 ContextView 后才对模型可见。

### D-007：Persistence 是不可关闭的事实基础设施

- 建议：确认。
- 含义：删除 `PersistencePolicy`；所有 Session、Run、Task/Flow 都有 append-only durable event log。只配置 backend 和 retention。

### D-008：允许一个窄 Context Decision Bridge

- 建议：确认并以 A0 证据为门。
- 含义：不建设通用执行 Bridge；只为自定义 Python ContextPolicy 连接 DSH request-derivation seam，增加 durable `request/context-view`，支持 REQUEST / TURN / SESSION 可见性并保持请求可重建。

### D-009：Task/Flow 在 A1 先持久化事件，A2 再实现恢复

- 建议：确认。
- 含义：A1 同时交付 `Sequence` 与最小 `PlannerReActFlow` 闭环并完整记录事件；A2 再增加安全 resume，不因暂不恢复而省略 Plan/Step/child Run 事实。

### D-010：新 namespace 隔离原型

- 建议：确认。

### D-011：保留 Host-owned Planner–ReAct Flow

- 建议：确认。
- 含义：DSH 拥有单个 Executor Run 内的 ReAct Agent Loop；Base Agent 拥有跨 Planner/Executor Run 的 Plan identity、revision、Step review、预算和终态。A1 为此提供 `PlannerDecision` / `StepOutcome` 最小结构化结果能力，但不重写 DSH 模型/Tool 循环。

## 17. 需要重点评审的选择

1. 是否确认 `ContextPolicy` 的普通业务权限只覆盖动态上下文，不允许删除安全 system instructions、当前输入和 Tool schemas？
2. 是否确认 Base Agent 必须提供 `REQUEST` / `TURN` / `SESSION` 三种可见范围，并为此前提增加一个窄 DSH Context seam；若上游未合入，原型是否允许携带精确 pin 的最小补丁？
3. 首版是否确认同时交付 `InMemoryMemory`（测试/开发）、`PostgresMemory`（参考持久实现）和 `ApiMemory` Adapter contract？
4. mandatory LocalState 是否提供默认 `.base-agent/state`，还是要求应用启动时显式传入目录？
5. 是否确认 `PlannerReActFlow` 属于 A1 核心 vertical slice，并把通用 structured result 留到 A3，而不是把 Plan/Step 所需的两个最小结构化契约一起推迟？

## 18. 变更记录

- 2026-08-28：完成第一轮双仓库研读，曾提出 Control Plane + 通用 Bridge 路线。
- 2026-08-28：确认 DSH 官方 Python SDK，方案改为 SDK-first v2。
- 2026-08-28：v2.1 恢复 Conversation History、Compaction 和 Persistence，但错误地把它们设计为三个并列 Policy。
- 2026-08-28：v2.2 将 History / Compaction / Memory selection 收敛为唯一 `ContextPolicy`；确认 DSH 没有内建长期 Memory；删除 `PersistencePolicy`，改为 Session / Run / Flow 强制事件持久化；为业务自定义 Python ContextPolicy 引入受限、可重建的 Context Decision seam。
- 2026-08-28：v2.3 明确 MCP Memory 不提供 Turn / Session 模型可见性控制；将 Memory availability 与 model visibility 分离，引入 REQUEST / TURN / SESSION `VisibilityScope`，并把 durable `request/context-view` seam 从“可能需要”调整为通用 ContextPolicy 的必要前提。
- 2026-08-28：补充 DSH Memory 的实际调用链：Memory Server 外置存储，`dsh-mcp-client` 只桥接普通 Tool；模型主动读写，`tool/result` 进入 Session surface，跨 Session 召回依赖外部 Store 而非 DSH 自动注入。
- 2026-08-28：v2.4 将公共 `MemoryStore` 更正为可插拔 `Memory` 能力 Protocol，只约定 recall / write；增加 `InMemoryMemory`、`PostgresMemory`、`ApiMemory`、Host-side `McpMemory` 实现边界和统一 `MemoryRuntime` 包装层。
- 2026-08-28：v2.5 恢复遗漏的 Host-owned `PlannerReActFlow`：DSH 负责单 Run 内 ReAct，Python Host 负责 Plan revision 与 create/execute/review 状态机；A1 增加 `PlannerDecision` / `StepOutcome` 最小结构化结果、Flow 事件和验收闭环，A2 再增加 durable resume。同时明确完整 `sdk` Profile 必须显式生成 composition/patch，不能与 Python SDK 隐式 bundled 默认配置混为一谈。
