---
marp: true
theme: default
paginate: true
size: 16:9
title: Base Agent 的设计思想与 Harness 架构选择
description: 从自身设计出发，与 Codex、DeepSeek Harness 横向比较
---

<!-- 正文约 19 分钟；压缩讲稿可控制在 17 分钟。 -->

# Base Agent 的设计思想与 Harness 架构选择

## 从自身设计出发，与 Codex、DeepSeek Harness 横向比较

> 先理解 Base Agent 为什么这样设计，再讨论这种选择得到什么、失去什么

<!--
约 1 分钟。

今天的分享分成两部分。前半段先介绍 Base Agent 自己的设计目标、核心原则和整体架构；后半段再通过几个问题，与 Codex 和 DeepSeek Harness 横向比较。

比较目的不是证明 Base Agent 功能更多，而是理解不同 Harness 对系统责任边界的选择，以及这些选择带来的优势和代价。
-->

---

# Base Agent 想解决什么问题？

它不是：

- 长期对话 Session
- Coding Agent 工作站
- 动态插件市场
- 让模型完全接管状态机的自治系统

它是：

> 一个可嵌入现有业务系统、以有限 Run 为执行边界、由 Host 掌握状态权威的 Agent SDK 与任务执行内核。

```text
明确输入 → 有限执行 → 结果 / 等待 / 失败 / 取消
```

<!--
约 1.5 分钟。

先明确定位。Base Agent 没有试图成为完整的 Agent Operating Environment，也不把 Agent 设计成长期存活、不断接收消息的 Actor。

它关注的是一次有限任务：从一个明确输入开始，经过模型与 Tool 执行，最终得到结果、进入等待、失败或取消。这个 Run 可以被 Flow 编排，也可以被 HTTP 和 Worker 执行，但普通开发者看到的仍然是一次任务。

因此它更适合数据分析、报告生产、审批流程和后台长任务，而不是长期聊天或 IDE 工作站。
-->

---

# 四个核心设计思想

| 设计思想 | 含义 |
|---|---|
| 显式窄腰 | 复杂能力不能扩大普通开发者主路径 |
| Host-owned | 模型生成内容，Host 提交状态和事实 |
| 运行语义冻结 | Run 开始后 Prompt、Tool、权限和策略不漂移 |
| Contract-first | 先固定边界与不变量，再增加默认机制 |

```text
Prompt + Tool + Skill
        ↓
AgentDefinition + Agent
        ↓
      Harness
        ↓
     RunResult
```

<!--
约 2 分钟。

Base Agent 的设计可以归纳成四点。

第一是显式窄腰。不管内部增加 Event、Checkpoint、Worker 还是恢复状态机，普通应用仍通过同一条主路径使用 Agent。

第二是 Host-owned。模型可以建议 ToolCall、Plan 和内容，但不能创建系统 identity、扩大权限、修改权威状态或自行接受产物。

第三是运行语义冻结。Run 开始后，Prompt、模型、Tool schema、Skill、权限、ContextPolicy 和预算会进入固定 snapshot。

第四是 contract-first。项目优先定义 Context、Tool、Event、Checkpoint、Artifact 等对象之间的边界和不变量，再提供内存或生产 Adapter。
-->

---

# 总体架构：窄腰内核 + 外围适配

```text
┌──────────────────────────────────────────┐
│ Application                              │
│ Prompt / Tool / Skill / AgentDefinition  │
│ starter / mtbi-analysis / 业务应用       │
└──────────────────┬───────────────────────┘
                   │ public API
┌──────────────────▼───────────────────────┐
│ Agent / Flow facade                      │
└──────────────────┬───────────────────────┘
                   │
┌──────────────────▼───────────────────────┐
│ Harness                                  │
│ snapshot / context / model-tool loop     │
│ governance / event / waiting / verify    │
└────────────┬──────────────────┬───────────┘
             │                  │
┌────────────▼──────────┐ ┌──────▼─────────────┐
│ Extension Ports       │ │ State Ports        │
│ Provider / Context    │ │ Event / Checkpoint │
│ Memory / Tool / Skill │ │ Artifact / Run/Flow│
└───────────────────────┘ └────────────────────┘
```

<!--
约 2 分钟。

从架构分层看，业务应用拥有 Prompt、Tool、Skill 和 AgentDefinition，并在 composition root 中选择 Provider 与 Store。

Agent 和 Flow 是公共门面。Harness 是包内运行内核，负责 snapshot、Context、模型与 Tool 循环、治理、等待和结果校验。

Provider、Context、Memory、Tool 和 Skill 是能力扩展端口；Event、Checkpoint、Artifact、Run 和 Flow Store 是状态端口。

关键约束是依赖方向：业务可以依赖公共 API，Harness 不能反向依赖业务 Flow、HTTP、Worker 或具体 Provider。starter 如果必须导入 base_agent.runtime，就意味着公共边界设计失败。
-->

---

# 一次 Run：模型循环被包在任务状态机里

```text
AgentDefinition
      ↓ freeze
HarnessSnapshot + fingerprint
      ↓
readiness → ContextView → ModelRequest
                         ↓
                  ToolCall proposed
                         ↓
          参数 → 权限 → 确认 → Tool
                         ↓
                Event / Artifact
                         ↓
            next turn / RunResult
```

```text
CREATED → RUNNING ⇄ WAITING
               ├→ SUCCEEDED
               ├→ FAILED
               └→ CANCELLED
```

**模型循环是实现细节，Run 才是业务执行边界。**

<!--
约 2 分钟。

一次 Run 启动时先冻结 HarnessSnapshot。readiness 在首次模型请求前完成；ContextPolicy 从原始 transcript 产生本次请求视图；模型提出 ToolCall 后，Host 依次完成参数、权限和人工确认检查。

执行过程中产生有序 Event，Tool 可以提交 Artifact；缺少用户输入时进入 WAITING 并保存 Checkpoint。最终 Run 进入明确终态。

这体现了 Base Agent 的核心层次：内部确实有 ReAct 式模型循环，但外层是一个任务状态机。业务关心的是这次任务是否成功、产物是什么、能否继续，而不是模型进行了多少次内部思考。
-->

---

# Flow 与生产执行面为什么在 Harness 外？

```text
Flow（Host-owned orchestration）
  ├── Agent Run
  ├── Host verify / handoff
  └── Agent Run

FastAPI Control Plane
          ↓
Run / Flow / Event / Checkpoint Store
          ↑
Execution Supervisor → Child → Harness
```

- Flow 组合多个普通 Run，不修改单 Agent 语义
- Planner 只能返回决策，不能拥有 Plan 状态机
- Server/Worker 负责部署和执行所有权，不进入 Agent API
- 本地模式与分布式模式共享相同逻辑结果

<!--
约 2 分钟。

多 Agent 和分布式执行都很复杂，但 Base Agent 有意把它们放在 Harness 外层。

SequentialFlow 和 PlannerReActFlow 组合普通 Agent Run。Host 负责阶段边界、验证、Artifact handoff 和状态 identity；Planner 只返回结构化决策。

生产执行面再把 FastAPI 控制面、持久 Store、Supervisor 和 Child 分开。Worker、lease 和 fencing 解决部署问题，但不会泄漏到 AgentDefinition 或普通 run() API。

这种分层的目标是：基础设施可以变复杂，但普通 Agent 开发方式不能随之变复杂。

到这里，Base Agent 自己的设计模型已经完整。下面再用横向问题检验这些选择。
-->

---

# 问题一：Harness 应对“任务结果”还是“会话连续性”负责？

|  | Base Agent | Codex | DeepSeek Harness |
|---|---|---|---|
| 一等对象 | Run / Flow / Attempt | Thread / Session / Turn | Agent / Session / Turn |
| 生命周期 | 有明确终态 | 长期 Coding Session | 长期交互式 Agent |
| 擅长回答 | 任务成功了吗？ | 当前工作区如何继续？ | 下一条消息进入哪里？ |
| 自然场景 | 后台任务、业务 Flow | Coding Agent | 对话、IDE、Agent OS |

**Base Agent 优势：** 终态、预算、计费、产物和恢复边界明确。

**Base Agent 劣势：** 不天然支持持续 followup、fork 和长期工作区体验。

<!--
约 2 分钟。

理解了 Base Agent，再问第一道选择题：Harness 最终应对任务结果，还是对会话连续性负责？

Base Agent 选择有限 Run，因此容易定义成功、失败、预算、SLA、Artifact 和恢复。

Codex 围绕 Coding Thread 与 Turn，DeepSeek Harness 围绕长期 Agent 和 Session，更自然地支持 followup、steer、fork 和持续工作区。

Base Agent 在后台业务任务上更直接，但如果用它做长期 IDE Agent，就必须在上层重新建设 Session；反过来，Session 型 Harness 如果要表达严格业务终态，也需要增加任务边界。
-->

---

# 问题二：语义应该冻结，还是允许动态变化？

| 方案 | 做法 | 优势 | 代价 |
|---|---|---|---|
| Base Agent | RunSnapshot + fingerprint | Run 级可复现、恢复证明简单 | 旧 Run 需要兼容 Runtime |
| Codex | 每个 Step 构造 StepContext | 请求与 Tool 执行精确一致 | 严格回放需保存有效配置 |
| DeepSeek | 插件树变化 + request header 日志 | Tool/MCP/HMR 动态适应 | 行为依赖 generation 与插件拓扑 |

```text
Base Agent：这个 Run 中不允许变化
Codex / DSH：允许在 Step 边界变化，但必须固定或记录
```

演进判断：**保留 RunSnapshot，在内部补充不可变 StepContext。**

<!--
约 2 分钟。

Base Agent 选择 Run-level semantic immutability。它的优势是审计、WAITING 恢复和跨 Worker 路由有共同身份；代价是发布后必须保留能够承接旧 fingerprint 的 Runtime。

Codex 在每次请求前构造 StepContext，让模型看到的 ToolRouter、环境和审批策略与之后执行时一致。DeepSeek Harness 允许插件、Tool generation 和 MCP 连接变化，再记录实际 request header。

这不是谁更严谨，而是静态可复现和动态适应的取舍。对 Base Agent 而言，RunSnapshot 与 StepContext 解决不同粒度的问题，合理方向是同时保留两者。
-->

---

# 问题三：复杂度应该放在哪里？

| 架构 | 优势 | 劣势 |
|---|---|---|
| Base Agent：显式窄腰 | 易嵌入、Provider 中立、调用路径可推导 | 新机制需要显式 Protocol，默认能力偏薄 |
| Codex：垂直产品内核 | Context、Tool、sandbox、UI 深度闭环 | 难脱离 Coding 产品对象图复用 |
| DeepSeek：Everything-is-a-plugin | 动态组合、scope、卸载、HMR | 全局行为依赖插件顺序和拓扑 |

```text
Base Agent 关注：宿主可以替换什么？
Codex 关注：完整产品路径能否一致工作？
DeepSeek 关注：能力能否动态组合与撤销？
```

<!--
约 2 分钟。

第三个问题是复杂度最终放在哪里。

Base Agent 把复杂度放进显式 Protocol、composition root 和状态边界，获得可嵌入性和静态可推导性，但新增横切机制通常需要修改明确接口。

Codex 把复杂度集中在垂直 Session Kernel，Context、Tool、sandbox、模型连接和 UI 能在同一主路径深度优化，但很难抽出一个轻量通用 SDK。

DeepSeek Harness 把复杂度分散到 plugin tree、service、effect、scope 和 waterfall。扩展速度快，但局部容易替换不等于全局行为容易理解。

Base Agent 的优势不是能力最多，而是业务应用不必接受整套产品对象图或插件世界观。
-->

---

# 问题四：契约正确，是否等于默认生产可用？

| 能力 | Base Agent | Codex / DeepSeek Harness |
|---|---|---|
| Context | source/request 分离正确；默认 KeepAll | token、pruning、compaction 更成熟 |
| Tool | 权限、确认、副作用契约明确 | 并发、取消、pipeline、sandbox 更完整 |
| Model | Provider 中立 | Session reuse、重试、fallback 更成熟 |
| 扩展 | Protocol 边界清晰 | StepContext 或插件截点更丰富 |

Base Agent 的真实优势：

- 架构边界、Provider 中立、Run 级可复现、Host-owned 状态

Base Agent 的真实不足：

- 默认 ContextPolicy、集中 ToolRuntime、真实 sandbox、ProviderSession

<!--
约 2 分钟。

最后一个问题用来避免只讲设计优点：契约正确，不等于默认实现已经生产成熟。

Base Agent 的 Context source/request 模型很好，但默认 KeepAll；Tool 有权限、确认和副作用分类，但并发、取消、sandbox 和脚本执行还没有完全收拢到统一 Runtime；Provider 协议中立，但连接复用、重试和 fallback 更多留给 Adapter。

Codex 的强项正是内循环工程成熟度。DeepSeek Harness 在长 Session、Tool pipeline 和动态扩展上也更完整。

因此 Base Agent 下一步不应继续横向增加概念，而应把已有契约做成默认可靠机制。
-->

---

# 总结：Base Agent 得到了什么，又失去了什么？

选择：

```text
显式窄腰 + 有限 Run + 语义冻结 + Host-owned 状态
```

得到：

- 可嵌入、可审计、Provider 中立、业务边界清楚
- Flow、Server、Worker 可以演进而不扩大 Agent 主路径

失去：

- 长期 Session、动态插件、成熟工作站和更强自治体验
- 默认 Context、ToolRuntime、sandbox 仍需工程补齐

> Harness 的设计起点不是“还缺什么功能”，而是“系统最终必须对什么负责”。

<!--
约 1 分钟。

最终，Base Agent 选择的是显式窄腰、有限 Run、运行语义冻结和 Host-owned 状态。

它得到的是可嵌入性、可审计性、Provider 中立和清楚的业务边界；它失去的是长期 Session、动态插件、成熟工作站和一部分自治能力。

所以分享的结论不应是 Base Agent 比另外两套更好，而是：它选择对一次有限业务任务的语义、状态和产物负责。后续演进应该吸收 Codex 与 DeepSeek Harness 已验证的机制，同时守住这一责任边界。
-->

---

# 参考材料

- [Codex 与 Base-agent：Harness 设计取向、工程能力与演进方向](https://moonton.feishu.cn/wiki/UWeowpDeXiGuqRkrsnecBRj5nVf)
- [Base Agent 与 DeepSeek Harness 架构对比解读](https://moonton.feishu.cn/wiki/Fs8pwuU6KikYZukruP3cErY2ndf)
- `ARCHITECTURE.md`
- `EXECUTOR_DESIGN.md`
- `docs/deepseek-harness-architecture-review.md`

<!-- 参考页不计入正文时间，可停留在本页进行问答。 -->
