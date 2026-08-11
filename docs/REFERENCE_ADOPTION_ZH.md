# AgentDemo、mock-manus 与 Hermes Agent 设计采纳矩阵

本文基于以下源码重新审计，不以抽象印象代替实现：

- AgentDemo：`/Applications/materials/CODES_NEW/pycharm/AgentDemo`
- mock-manus：`/Users/huangsien/PycharmProjects/mock-manus`
- Hermes Agent：`/Users/huangsien/PycharmProjects/hermes-agent`

目标不是复制两个项目，而是找出哪些设计能帮助 `base-agent` 成为更好的 Agent SDK：

> 开发者主要编写 Prompt、Tool、Skill 和少量 Agent/Flow 组合；Harness 在背后统一承担
> 运行、观察、验证和治理。

## 1. 总体结论

之前的提炼偏向 Run、Event、Worker 和恢复机制，低估了参考项目在 **Agent 角色表达、
Prompt 组织、Tool 能力包、阶段化 Flow、Planner/Executor 闭环、上下文预算和实时事件
体验** 上的价值。

`base-agent` 已经拥有不少更稳健的底层实现，但这些实现没有转化为同样直观的开发接口。
下一阶段应优先补齐 SDK 表达能力，而不是继续增强 durable worker。

三套参考实现的价值不同：

- AgentDemo 帮助定义业务 Agent、阶段化 Flow 和 Planner/Executor 的直观表达；
- mock-manus 帮助定义实时事件、任务资源、等待恢复和 Planner/ReAct 闭环；
- Hermes 帮助定义 SDK 的窄腰边界、稳定的模型上下文、能力渐进披露和扩展治理。

## 2. 应进入 SDK 主路径的设计

| 参考设计 | 源码体现 | 当前状态 | 采纳决定 |
| --- | --- | --- | --- |
| 薄 Agent 角色 | AgentDemo 的 `MTBIAgent`、`DataAnalysis`；mock-manus 的 `PlannerAgent`、`ReActAgent` 主要声明角色 Prompt、Tool 和输出方式 | `AgentDefinition` 已声明 instructions、Tool、Skill 和预算，但缺少 description、Tool choice、结构化输出和清晰的上下文策略 | 扩展声明式 AgentDefinition，不复制业务 Agent 继承树 |
| Prompt 与运行代码分离 | AgentDemo `app/prompt/`；mock-manus `domain/services/prompts/` | starter 和示例仍有大量内联 Prompt；没有推荐 Prompt asset loader | 提供简单 `load_prompt()` 和推荐 `prompts/` 目录，不引入 Prompt DSL |
| Tool 能力包 | AgentDemo `ToolCollection`；mock-manus File/Shell/Browser/Search Tool 组 | 已有 ToolRegistry、Bundle 和 Toolkit factory，但开发者定义自有 Toolkit 的体验不统一 | 提供小型不可变 `Toolkit` 组合对象；注册、启用和权限仍保持分离 |
| Agent description 用于选择 | AgentDemo `PlanningFlow` 把 Agent name/description 提供给 Planner | `AgentDefinition` 没有 description；Flow 只能依赖 key 和固定顺序 | 将 description 加入 AgentDefinition，供 Router/Planner 使用但不授予能力 |
| Tool choice | 两个项目都区分 NONE/AUTO/REQUIRED | Provider 模型已有 ToolChoice，但 AgentDefinition 不能声明角色级策略 | 在 AgentDefinition 中增加 provider-neutral tool choice |
| 结构化输出 | mock-manus Planner/ReAct 使用 JSON output；AgentDemo PlanningTool 使用结构化 Tool 输入 | Skill 有 output schema，但 Agent 自身没有稳定 output contract | 为 Agent 增加可选 response/output schema，解析和验证由 Harness 完成 |
| 有界观察结果 | AgentDemo `max_observe`；mock-manus 的记忆压缩 | 已有 ToolResult 持久化大小上限，但“写入下一轮模型上下文的观察大小”没有独立策略 | 增加 Context/Observation policy，不能把持久化上限等同于模型上下文策略 |

## 3. 应进入简单 Flow API 的设计

### 3.1 阶段不是只有 Agent 顺序

AgentDemo `MTBIAnalysisFlow` 的一个阶段实际包含：

- 目标 Agent；
- 针对该阶段的 Prompt；
- 原始用户输入如何进入阶段；
- 预期 SQL/CSV/Report 产物；
- 阶段完成条件，例如 CSV 是否真实产生；
- 下一阶段只接收什么输入；
- 最终结果如何汇总。

当前 `Flow.sequence({"mtbi": agent1, "analysis": agent2})` 只表达 Agent 顺序，并把同一
原始 Prompt 交给所有 Agent。它没有真正采纳 `MTBIAnalysisFlow` 的核心价值。

应增加一个简单、声明式的阶段对象，目标使用方式接近：

```python
flow = Flow.sequence(
    id="mtbi-analysis",
    stages=[
        Stage(
            name="query",
            agent=mtbi_agent,
            prompt=load_prompt("prompts/query.md"),
            output=ArtifactOutput(name="query-data", media_type="text/csv"),
        ),
        Stage(
            name="analysis",
            agent=analysis_agent,
            prompt=load_prompt("prompts/analysis.md"),
            input_from="query",
        ),
    ],
)
```

这里的重点不是立即固定这些类名，而是让阶段 Prompt、输入映射、输出契约和验证条件成为
显式数据，不能继续隐藏在 Flow 子类的 Python 字符串拼接里。

### 3.2 Planner/Executor 是标准预设

AgentDemo `PlanningFlow` 和 mock-manus `PlannerReActFlow` 共同说明了一个稳定闭环：

```text
创建 Plan
→ 根据 Agent description 或固定角色选择 Executor
→ 执行一个 Step
→ 更新或重规划
→ 重复执行
→ 总结
```

`base-agent` 已有 ExecutionPlan、PlanningStrategy 和 ReActStrategy，但开发者仍无法用
两个普通 Agent 简单表达这个分工。应在阶段化顺序 Flow 稳定后提供
`Flow.planner_executor(...)` 预设，而不是先暴露 FlowLifecycle、AgentInvoker 或新的
Factory Enum。

### 3.3 Flow 应支持实时观察

mock-manus `BaseFlow.invoke()` 直接返回异步 Event 流；AgentDemo 虽未形成同样完整的事件
模型，但运行过程对阶段和 Tool 有明显日志反馈。

`base-agent` 的 Agent `RunHandle.stream()` 已经具备正确方向。简单 Flow 应提供等价的
`start()`/`stream()` 体验，而不是要求开发者先执行完再调用 `events()`。

## 4. 应由 Harness 吸收的设计

| 参考能力 | 正确归属 | 采纳方式 |
| --- | --- | --- |
| 有界 step loop、重复检测 | Runtime/Supervisor | 已基本采纳，普通 Agent 不需要重写 `step()` |
| Tool schema、参数和统一结果 | Tool Harness | 已有更强的类型推导、权限、超时和结果边界 |
| WAITING/人工输入 | Runtime/Run | 已以 WaitForInput、Checkpoint、resume、confirm 采纳；不复制 Tool 内 `input()` |
| Agent/Flow 状态和事件 | Runtime/Event | 保留类型化、有序、可回放事件；补 Flow live stream |
| Resource cleanup | ResourceManager | 已统一管理；不复制每个 Agent 手写 cleanup |
| Attachment 与生成文件 | Attachment/Artifact | 已有核心契约；阶段 Flow 需要能声明输入输出引用 |
| 上下文压缩 | Context policy | 从 mock-manus 的步骤后 compaction 提炼为策略，不写死在 ReAct Agent |
| 计划结果验证 | Verification | 从 MTBI Flow 的“CSV 必须存在”提炼为 Stage/Outcome Verifier |
| Tool 前后业务 Hook | Tool/Context middleware 或业务 Tool | 从 MTBIAgent metadata registry 提炼稳定扩展点，避免为每个领域继承 Agent |
| Event 到 UI 内容转换 | 宿主 Presenter/Task adapter | 借鉴 mock-manus TaskRunner，但不把 Browser screenshot 等业务 Event 放进核心 |

## 5. Hermes 带来的关键架构修正

### 5.1 保持窄腰，而不是把每种能力都做成核心 Tool

Hermes 的 toolset、Skill、plugin、MCP 和 core Tool 形成了一条“能力占用阶梯”。它提醒
`base-agent`：每新增一种能力，都会增加公共 API、模型可见 schema、上下文占用和治理
成本。

`base-agent` 应采用更适合 SDK 的顺序：

```text
扩展已有 Prompt/Tool
→ 组合为 Skill/Toolkit
→ 使用可选 Adapter 或独立集成包
→ 最后才考虑进入核心 Tool/API
```

这不是要求 `base-agent` 立即实现 Hermes 的插件市场。近期只需确保根 API 保持小，
Toolkit/Skill 是常规扩展路径，Provider、MCP 和基础设施实现留在明确的扩展模块。

### 5.2 一次运行必须固定模型可见的 Harness 快照

Hermes 会按稳定、上下文和易变内容分层构造系统 Prompt，并在一次会话内缓存；动态 Tool
目录也不能随意改变已经缓存的系统 Prompt。

`base-agent` 应把这个原则提升为通用契约：

- `AgentDefinition`、解析后的 Prompt、启用的 Skill、Tool schema、模型配置和治理策略
  在 `Run` 开始时形成不可变的 `HarnessSnapshot`；
- 同一 `Run` 中不能因为全局注册表变化而悄悄新增或删除模型可见 Tool；
- Context selection/compaction 可以改变会话内容，但必须产生事件，不能暗中修改角色定义；
- snapshot/version 应进入 Run 元数据，以支持复现、审计和评估。

这比复制 Hermes 的全局注册表更重要。`base-agent` 不应采用 import-time 自注册和运行中
动态覆盖 Tool 的方式。

next 原型的 `SkillCatalog.discover_trusted()` 不是全局自注册：它只在应用显式
传入的受信任 root 中导入固定 `tools.py`，将本地 `@tool` 收集到该 Catalog，并在
Agent 构造时根据显式 Skill 选择固定 Tool schema。外部 Skill 仍使用只读 `discover()`，
运行中不动态覆盖 Tool 集。

### 5.3 Tool readiness 与 Toolkit 是两个不同概念

Hermes 的 Tool registry 支持 `check_fn`、环境依赖和按场景选择 toolset。可采纳的不是
全局 registry 结构，而是“声明能力”和“验证当前可用性”分离：

| 概念 | 回答的问题 |
| --- | --- |
| `Toolkit` | 这个 Agent 被配置了哪些 Tool？ |
| `ToolReadiness` | 这些 Tool 在当前环境中是否真的可用，缺少什么？ |
| `GovernancePolicy` | 当前主体是否有权调用，是否需要确认？ |
| `ToolExecutor` | 通过以上检查后，如何隔离、超时并执行？ |

Readiness 应在启动或 Run preflight 阶段确定，并可被测试和观察。普通业务 Agent 不应在
执行到一半后才从错误字符串中发现浏览器、凭据、二进制或远程服务不可用。

### 5.4 Skill 和 Tool 目录需要渐进披露

Hermes 先给模型 Skill 索引，只在明确加载后提供完整 `SKILL.md` 和支持文件；Tool Search
也会在目录过大时只暴露概要，再按需提供 schema。

`base-agent` 应先建立以下语义，不急于复制完整搜索工具：

- Skill 可以包含说明、references、templates、assets 和 scripts；
- Skill metadata、完整指令和支持资源是不同披露层级；
- Skill/Tool 可以声明 prerequisite 和 readiness；
- Prompt、Skill、Tool schema、历史消息和 Tool observation 分别计入上下文预算；
- 只有在真实应用的 Tool/Skill 数量达到规模后，再实现 Tool Search 或动态 schema 披露。

Skill 中的脚本执行仍须经过 Tool 权限、隔离和确认，不能把 Hermes 的内联 shell
预处理直接复制到核心 Prompt loader。

### 5.5 Observer、Middleware 与 Governance 必须严格分开

Hermes 的文档已经区分 observer 和 middleware，但仍保留少数历史 Hook 可以修改或阻断
行为。`base-agent` 应采用更严格的新契约：

| 扩展面 | 权限 | 失败语义 | 示例 |
| --- | --- | --- | --- |
| `Observer` | 只读、不能改变执行 | fail-open | trace、metrics、audit export |
| `Middleware` | 显式变换请求/结果或包装执行 | 由声明的链策略决定 | request shaping、cache、routing |
| `GovernancePolicy` | allow/deny/confirm、预算和数据边界 | fail-closed | Tool 权限、副作用确认、访问控制 |
| `Verifier` | 判断前置条件、产物或轨迹是否满足契约 | 产生结构化结论 | Stage output、Artifact、测试证据 |

推荐执行顺序为：

```text
构造请求
→ Middleware 形成 effective request
→ Governance/Readiness 检查 effective request
→ 执行
→ Verifier 记录结果证据
→ Observer 接收脱敏后的事实事件
```

Observer 的异常不得中断 Agent。Middleware 是否 fail-open 必须由注册时的显式策略决定；
不能因为扩展抛错就无条件绕过一个本应生效的安全变换。

### 5.6 Context 是有预算的运行资源

Hermes 将上下文选择、压缩和旧 Tool 结果裁剪分开，并对单个 Tool 结果、每轮累计观察和
最终上下文分别设限。这比一个统一的 `max_result_size` 更准确。

`base-agent` 后续应区分：

1. Tool 原始结果的持久化上限；
2. 写回模型上下文的 observation 上限；
3. 单轮全部 Tool observation 的累计预算；
4. Prompt、Skill、Tool schema 和历史消息的整体预算；
5. 超限后是截断、压缩、拒绝还是外部化为 Artifact。

大结果外部化仍是高级、显式 opt-in 策略。应用必须指定 media type、访问范围和保留
策略后，Harness 才能生成 Artifact 引用；它不是当前简单 Agent/Flow API 的前置条件。

### 5.7 可验证性需要证据，而不只是一个布尔值

Hermes 的 verification evidence 会记录实际执行了什么检查、作用范围、退出状态和有限
输出，并明确避免把局部检查夸大成“整个仓库通过”。这个原则适合进入
`base-agent` 的 `VerificationResult`/`Evidence` 设计：

- 证据包含 verifier、scope、status、时间和关联的 Run/Stage/ToolCall；
- 证据内容默认有界和脱敏；
- “未验证”“局部通过”“完整通过”必须可区分；
- Verifier 只记录和判断，不自行决定扩大执行范围。

### 5.8 仅留在宿主或产品层的 Hermes 能力

以下设计可以作为未来宿主应用参考，但不进入当前 SDK 核心：

- CLI、TUI、Gateway、消息平台和 cron；
- profile builder、按平台/聊天/线程/用户进行 profile 路由；
- 插件安装、市场、自动发现和运行中覆盖；
- voice、desktop、dashboard 和浏览器产品体验；
- 后台自动编写或修改 Skill；
- 产品级会话目录、密钥目录和多平台 session key 规则。

核心只需要稳定的 Run/Conversation identity、Provider/Resource 协议和显式 composition
root，不承担完整 Agent 产品的配置与路由系统。

## 6. 已经正确采纳的部分

- Provider、Tool、Store、Resource 使用协议隔离；
- Agent 执行有预算、取消、无进展检测和统一终态；
- Tool 参数验证、权限、超时、副作用声明和确认；
- Run 与有序 Event；
- WAITING/resume；
- ExecutionPlan、ReAct 和 Planning；
- Attachment、Artifact 和执行期 Resource；
- Tool、Skill、Agent、Flow 的离线测试 Harness；
- 内存默认实现和可选基础设施 Adapter。

这些底层能力应继续保留，但通过简单 Agent/Flow API 自动使用。

## 7. 明确不复制的实现

| 参考实现 | 不复制原因 | base-agent 方向 |
| --- | --- | --- |
| `BaseAgent → ReActAgent → ToolCallAgent → 业务 Agent` 深继承 | 业务扩展容易被迫理解和覆盖运行循环 | AgentDefinition + Strategy + Toolkit 组合 |
| 可变 Pydantic Agent/Flow 对象直接充当运行控制器 | 配置、运行状态和持久化状态混在一个对象 | 不可变 Definition 与 RuntimeContext 分离 |
| Flow 直接创建 LLM、Tool、存储和外部资源 | 业务流程难测试、难替换基础设施 | 应用 composition root 注入 |
| `FlowFactory` 在核心中使用固定 Enum | 新 Flow 必须修改 Factory | 可选应用工厂；核心使用构造器和扩展协议 |
| Tool 内直接调用 `input()` | 无法用于 HTTP、Worker 或恢复 | WaitForInput + resume |
| `Terminate` Tool 控制 Runtime 状态 | 模型通过业务 Tool 操纵生命周期 | Strategy 返回完成决策，Runtime 提交终态 |
| Tool 异常转换成普通字符串 | 观察、治理和重试无法区分错误类型 | 结构化 ToolResult 和错误码 |
| 业务 Flow 用文件是否存在直接控制核心状态 | 仅适用于单机工作区 | 通用 Artifact/Outcome Verifier，业务规则留在应用 |
| 每个 Agent 自行 cleanup | 容易遗漏或重复释放 | ResourceManager 统一作用域 |
| Hermes 的超大 `AIAgent` 构造器和集中式 agent loop | 参数、产品配置和核心运行逻辑高度耦合 | 小型 AgentDefinition；Harness 通过内部 RuntimeContext 组装 |
| import-time 全局 Tool 自注册、自动发现和覆盖 | 测试隔离困难，也会使运行中可见能力漂移 | composition root 显式组装，并在 Run 开始时冻结 snapshot |
| 把大量产品 Tool 加入核心 toolset | 增加 schema、依赖、权限和维护成本 | Skill/Toolkit/可选集成包优先，核心保持窄腰 |
| Observer Hook 兼具阻断或结果变换能力 | 观察者和行为控制无法独立推理 | Observer、Middleware、Governance、Verifier 分契约 |
| Skill Prompt 预处理直接执行任意 shell | Prompt 加载绕过 Tool 治理和执行隔离 | 脚本作为受治理 Tool/Resource 执行 |
| 默认支持插件覆盖现有 Tool | 覆盖授权、兼容和审计成本过早进入 SDK | 真实插件生态出现后再设计，当前要求显式注册冲突失败 |
| profile/gateway/session 路由进入 Agent 核心 | 属于产品宿主的用户与渠道语义 | 核心只保留通用 identity 和上下文生命周期 |

## 8. 对当前路线的修正

按参考项目重新排序后，SDK 下一阶段应依次处理：

1. 补齐 AgentDefinition 的角色表达：description、Prompt asset、tool choice、结构化输出；
2. 定义 Run 启动时固定的 Prompt/Skill/Tool/Harness snapshot；
3. 稳定自定义 Toolkit 的组合体验，并增加显式 readiness preflight；
4. 设计带阶段 Prompt、输入映射、输出契约和 Verifier 的简单 Sequential Flow；
5. 给 Agent/Flow 提供一致的 start/stream 体验和只读 Observer 事件；
6. 再提供 Planner/Executor 预设；
7. 用 AgentDemo 的 MTBIAnalysisFlow 和 mock-manus 的 PlannerReActFlow 做兼容性验证；
8. 只有在真实目录和输出规模达到阈值后，再实现 Tool/Skill 渐进搜索和 Artifact overflow。

在这些工作完成之前，不继续扩展 durable worker、复杂恢复和并行多 Agent。
