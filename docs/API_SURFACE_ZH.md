# Agent SDK 公共 API 盘点与收口基线

本文是 [`ARCHITECTURE_DESIGN_ZH.md`](ARCHITECTURE_DESIGN_ZH.md) 中三层 API 可见性的执行
清单。它只决定“开发者从哪里看到和导入能力”，不要求删除已经实现的 Harness 能力。

## 1. 当前问题

当前导出规模：

| 入口 | `__all__` 数量 | 判断 |
| --- | ---: | --- |
| `base_agent` | 246 | 同时混入 SDK、扩展接口、基础设施和内部状态机 |
| `base_agent.flows` | 81 | 简单 Flow 门面和 durable worker 内核没有隔离 |
| `base_agent.tools` | 28 | Tool 作者 API 与执行/治理实现混合 |
| `base_agent.models` | 34 | 主要属于 Provider/Adapter 扩展契约 |
| `base_agent.stores` | 23 | 主要属于基础设施扩展契约 |

根包导出数量本身不是唯一问题。真正的问题是普通开发者无法从导入路径判断一个类型属于：

- 编写 Agent 时每天使用的稳定 SDK；
- 实现 Provider、Strategy 或 Store 时才使用的扩展协议；
- 仅由 Harness 和生产 Adapter 使用的执行内部结构。

因此，后续使用“根包白名单”管理 SDK 主路径。未进入白名单的类型不一定删除，但不再作为
入门 API 推荐。

## 2. SDK 主路径白名单

目标 `from base_agent import ...` 只保留以下类别。

### 2.1 Agent

| 类型 | 用途 |
| --- | --- |
| `AgentDefinition` | 声明 Prompt、Tool、Skill、权限和预算 |
| `Agent` | 组合并执行一个 Agent |
| `AgentResult`、`AgentResultStatus` | 获取一次执行的业务结果 |
| `RunHandle`、`RunStatus` | 控制和观察后台执行 |

`AgentProfile` 仅作为兼容类型保留，不进入新应用推荐路径。`AgentRuntime`、`RuntimeContext`
和 `RuntimeServices` 属于 Harness/Strategy 扩展，不属于普通 Agent 构造 API。

### 2.2 Tool 与治理声明

| 类型 | 用途 |
| --- | --- |
| `tool`、`Tool`、`ToolContext` | 定义同步、异步或上下文感知 Tool |
| `ToolResult`、`ToolResultStatus` | 返回或检查结构化 Tool 结果 |
| `WaitForInput`、`PendingInput` | 显式进入人工等待 |
| `ToolSideEffectMode` | 声明只读、非幂等或下游幂等副作用 |
| `ToolConfirmationMode` | 声明是否需要执行前确认 |
| `ToolConfirmation`、`ToolConfirmationDecision` | 提交类型化确认决定 |

`ToolExecutor`、`ToolRegistry`、结果策略、确认策略和 recorder 属于 Harness 扩展或内部实现。
普通 Tool 作者不需要构造它们。

### 2.3 Skill

| 类型 | 用途 |
| --- | --- |
| `Skill`、`SkillManifest` | 定义可复用任务知识 |
| `SkillRegistry` | 从应用目录加载和选择 Skill |

`SkillLoader` 和校验器属于扩展接口，不进入最小主路径。

### 2.4 Flow

| 类型 | 用途 |
| --- | --- |
| `Flow` | 组合命名 Agent |
| `FlowRun` | 获取输出、等待、确认、取消和事件 |
| `FlowBudget` | 可选的 Flow 总预算 |

`FlowDefinition`、`FlowStrategy` 和 `SequentialFlowStrategy` 属于自定义 Flow 的扩展层。
Repository、Lifecycle、Invocation、Lease、Worker、Recovery、Review 和 SideEffect Ledger
全部属于 Harness 内部或生产 Adapter，不从根包平铺暴露。

### 2.5 通用输入输出

| 类型 | 用途 |
| --- | --- |
| `Attachment`、`Artifact` | 输入附件和受控结果引用 |
| `TokenUsage` | 读取模型用量 |

`Message`、`ModelRequest`、`ModelResponse`、`ToolCall` 和 `ToolDefinition` 主要服务于
Provider、测试替身和 Adapter，统一从 `base_agent.models` 导入。

### 2.6 版本

`__version__` 保留在根包。

## 3. 扩展接口的目标导入路径

扩展开发者仍然可以使用稳定协议，但必须通过能说明职责的子模块导入：

| 扩展任务 | 目标入口 |
| --- | --- |
| 实现模型 Provider | `base_agent.providers`、`base_agent.models` |
| 实现自定义 Tool 执行/治理策略 | `base_agent.tools` |
| 实现 Skill 加载或校验 | `base_agent.skills` |
| 实现 Agent Strategy 或 Plan 执行 | `base_agent.orchestration` |
| 实现自定义 Flow Strategy | `base_agent.flows` 的扩展契约 |
| 实现 Run/Event/Checkpoint Store | `base_agent.stores` |
| 实现 Resource | `base_agent.resources` |
| 使用 Web、Data、Coding 能力包 | 对应 `web_search`、`data_sources`、`toolkits` 子模块 |
| 编写离线测试 | `base_agent.testing` |
| 接入 PostgreSQL、Redis、MCP、Sandbox、Browser | 对应可选 Adapter 子模块 |

Provider、Store 和 Adapter 类型不应因为“常用”就重新平铺回根包。SDK 主路径稳定性优先于
少写一段模块路径。

## 4. Harness 内部能力

以下能力可以继续存在，也可以由生产 Adapter 使用，但不属于 Agent SDK 主路径：

- `AgentInvocation*`、`AgentInvoker`、`AgentRuntimeInvoker`；
- `FlowRunState`、`FlowLifecycle`、`FlowRepository`；
- `FlowExecutionLease`、`FlowExecutionRunner`、`FlowLeaseRepository`；
- `FlowWork*`、`FlowPollingWorker`；
- `FlowRecovery*`、`DefinitionResolvingFlowWorkHandler`；
- `FlowWorkReview*`；
- `FlowSideEffect*`、`FlowToolSideEffectRecorder`；
- 上述能力的内存/PostgreSQL 实现和内部异常。

这些类型后续应通过具体模块导入，例如 `base_agent.flows.repository`、
`base_agent.flows.worker` 或对应 PostgreSQL Adapter。它们不应出现在 Quick Start、
Agent/Tool/Skill 教程或根包自动补全中。

## 5. 兼容性收口顺序

公共面不能一次性破坏。后续按以下顺序执行：

1. 先把 README、Getting Started、示例和 starter 全部改为只使用 SDK 白名单；
2. 把核心包内部和测试改为从真实所属子模块导入；
3. 为扩展接口补齐稳定的子模块导出；
4. 从根包 `__all__` 移除内部类型，并按项目兼容策略决定是否保留临时转发；
5. 增加 API surface 测试，防止内部类型再次泄漏到根包；
6. 最后清理不再需要的兼容转发，底层实现是否删除另行评估。

## 6. 本轮结论

当前首要问题是 **API 可见性失控**，不是必须推倒现有 Harness。下一步先设计并验证最小
Agent/Flow 接口，再按白名单收敛导出；在此之前不继续增加 Worker、Recovery、复杂 Flow
或分布式执行能力。
