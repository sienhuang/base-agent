# 设计决策

## D-001：业务框架完全本地拥有

Agent、Context、Memory、Tool、Skill、Flow、Run、Event 和 HTTP 类型全部定义在
`dsh_base_agent` 包内。安装依赖中不包含其他 Agent 框架。

## D-002：每层只有一个执行 Owner（由 D-011 补充）

业务 Flow 由 Host 编排，单 Agent 模型循环由 DSH 编排。Host 不解释或重放 DSH 的
模型步骤。确定性业务 Flow 由 Host 控制；需要模型自主拆解和调度子 Agent 的 Flow 可以
编译为 DSH Workflow/Subagent，不能把两者混为一类。

## D-003：Context 和 Memory 分离

Memory 只返回候选；ContextPolicy 选择候选并提交可审计 decision。只有已提交引用对应的
内容可以进入 DSH 输入。

## D-004：Tool 通过 loopback MCP 进入 DSH

Python Tool 仍由应用拥有。ToolGateway 保留输入验证、readiness、权限、确认、超时和
观测大小预算，DSH 看到的是普通 MCP Tool。

## D-005：Skill 是业务能力，不是执行引擎（由 D-011 修订）

Skill 的业务定义、依赖、权限和 Tool 贡献由应用拥有，但不能长期把全部 playbook 编译进
persona。后续应由 DSH Skill Registry、Skill Provider 和 `tool-skill` 提供目录与按需加载；
脚本执行必须通过明确的 DSH/Host 受控执行适配器开放。

## D-006：不可变 fingerprint 隔离 Runtime

定义、模型、ContextPolicy、Memory、Tool、Skill、治理策略或预算变化都会产生新的
fingerprint，从而使用独立 DSH home 和 Runtime。

## D-007：事件是统一事实流

Context、Memory、Tool、DSH、Agent、Flow 和 HTTP 查询共享一个有序 Event 模型。当前
JSONL 实现保证单进程追加和 fsync，但生产环境必须替换为并发安全的持久化实现。

## D-008：DshApplication 是唯一业务装配入口

业务代码只注册 Context、Memory、Tool、Skill、Agent 和 Flow。Provider Catalog、DSH
Runtime factory、LocalState、FastAPI 和关闭顺序由 `DshApplication` 统一拥有。底层
`BaseAgentApp` 和 `_dsh` adapter 保留为框架内部构件。

## D-009：Context 是命名的一等能力

`Context` 将 ContextPolicy、ContextBlock、MemoryScope、Memory 写回和 token 预算绑定为
一个不可变业务配置。Agent 通过名字选择 Context，避免在每个 Agent 构造点重复散落参数。

## D-010：Provider 使用命名配置

`ProviderConfig` 将业务使用的配置别名与 DSH provider/model 路由分离。这里的名称只是
应用侧 Provider 别名，不是 DSH Profile。默认配置可从环境读取，多 Provider 服务显式注册
多个配置。凭证只在内存中传递给 SDK，不进入 Event、snapshot 或 repr。

## D-011：DSH 必须是 Agent Runtime 核心，而不是模型调用包装器

### 架构审计结论

当前实现已经真实使用 `DeepSeekHarness`、完整 `sdk` Profile、patch、Provider/Model 参数、
进程复用、DSH 模型循环、MCP Tool 调用、部分 compaction 和 `session.event`。因此它不是
一个伪 DSH 集成。

但当前总体形态仍然更接近“Python Agent 框架 + DSH 执行适配器”：Context、Memory、Skill
和 Flow 的大部分运行语义由 Python 重新实现，DSH 的 Session、Skill、Workflow、Subagent、
插件和完整通知能力没有系统接入。后续不得继续简单堆叠 Host 层逻辑，必须建立从业务能力
定义到 DSH Runtime 组合的显式编译边界。

### 必须保留的业务边界

业务开发者仍然只定义或实现 Context、Memory、Tool、Skill、Agent 和 Flow，不需要了解
Cordis、DSH 插件安装、JSON-RPC 子进程或 patch 文件格式。`DshApplication` 继续作为业务
装配入口。

### 必须重构的 Runtime 边界

新增 `DshCapabilityCompiler`（最终名称可调整），负责把业务定义编译为：

- DSH Profile 选择与有序 patches；
- MCP Server 与 Tool 暴露策略；
- DSH Skill Provider、Catalog 和 `tool-skill`；
- Session、compaction 和动态 Context 配置；
- 可选的 Workflow/Subagent 能力；
- 完整的事件、通知和子 Agent 祖先关系映射。

### 分能力结论

1. **Profile 与 Provider 必须分离**：`ProviderConfig` 只描述模型路由、凭证和模型参数；另行
   定义 `DshProfileConfig`/`HarnessConfig`，描述 `profile`、`dsh_home`、`patches`、插件、
   timeout 和原生能力策略。当前硬编码 `profile="sdk"` 只能作为默认值。
2. **Session 必须成为一等概念**：提供稳定的 Conversation/Session API，把业务会话 ID
   映射为 DSH `session_id`。不能每次请求都生成新 Session，否则 DSH 持久对话、Session
   compaction、Session 资源与子 Agent 关系都无法生效。
3. **Context 必须控制真实会话上下文**：ContextPolicy 不能永远只看到 `turn=1` 和当前
   prompt。业务 Context 选择与 DSH Session 历史、compaction 应协作；动态业务资料可以在
   Host 选择，但要通过明确的 Context 编译/注入协议进入 DSH。
4. **Memory 与 Session Memory 分层**：DSH Session 持久化负责会话历史；业务 Memory 负责
   跨会话、租户隔离的长期记忆。业务 Memory 可通过 Context Provider 或受控 MCP 接入，
   不能把两者混成同一个存储概念。
5. **Tool 继续走 MCP**：Python Tool 经 loopback MCP 交给 DSH 调度的方向正确。原生 DSH
   Tool 应使用逐项 allowlist/策略配置，不能只提供 `allow_native_tools` 总开关。
6. **Skill 使用 DSH 原生能力**：Skill 定义仍归业务所有，但运行时应接入 DSH Skill
   Registry/Provider/`tool-skill`，支持目录、按需加载及 compaction 后恢复，不再默认把全部
   playbook 塞进 system prompt。
7. **Flow 分为两类**：确定性、可审计的业务 `HostFlow` 保留在 Python；模型自主规划、并行
   或子 Agent 调度使用 `DshWorkflow`/Subagent。注册时必须显式选择执行 Owner。
8. **通知不能只保留根 Session**：适配层需要保存 SDK 的完整 notifications、根 events、
   finish reason 及子 Agent 祖先关系，统一映射到框架 Event，不应按根 `session_id` 提前丢弃。

### 重构验收标准

重构完成后，应能通过集成测试证明：同一业务会话连续调用复用同一 DSH Session；Skill
由 DSH 按需加载而不是预先拼入 persona；Python Tool 仍由 DSH 经 MCP 调用；Context 与
compaction 在真实多轮历史上生效；Agentic Flow 能产生并保留子 Agent 通知；业务代码无需
直接构造 `DeepSeekHarness`、Profile patch 或插件配置。

## D-012：dsh-base-agent 是公司级 Agent 应用平台和控制面

本决策是项目的总架构原则；若前述设计决策与本节冲突，以本节为准。

`dsh_base_agent` 不重新实现 Agent Runtime。DSH 是底层执行引擎，负责模型循环、Tool
调用、Session、Skill、Workflow/Subagent 和上下文压缩。`dsh_base_agent` 负责公司统一的
Python SDK、稳定业务 API、任务管理、租户权限、审计和生产治理。

### 两个真相源

- DSH Session/Event 是 Agent 实际执行过程的真相。
- base-agent Run/Audit 是公司业务任务及治理状态的真相。

base-agent 保存业务 Run 与 DSH Session 的映射，可以转发、索引和提取 DSH Event 中的
审计事实，但不得复制 DSH 的全部内部消息并维护第二套模型对话状态机。

### 核心数据关系

```text
Tenant
  └── AgentDefinition + Version
        └── Run（业务任务）
              ├── RunAttempt（实际执行尝试）
              │     └── DSH Session ID
              ├── Audit Log
              ├── Artifact
              └── Approval / Waiting
```

同一次等待后继续或安全的基础设施恢复复用原 RunAttempt 和 DSH Session；用户主动重试或
完整重跑创建新的 RunAttempt 和 DSH Session。Run 始终是业务调用方看到的稳定任务身份。

### 边界判断

如果代码在决定“下一轮给模型发送什么、怎样调用 Tool、怎样压缩上下文”，它属于 DSH；
如果代码在决定“谁能运行、任务是否排队、是否超额、是否审批、怎样审计以及怎样满足
SLA”，它属于 `dsh_base_agent`。

`next` 只能作为生产控制面能力的零件库：可选择性迁移 Worker、lease、heartbeat、
EventStore、Checkpoint、Run 状态机和 Artifact 等能力；不得迁移其 Python Harness、
模型循环或另一套 Agent Runtime。
