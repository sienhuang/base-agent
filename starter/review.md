# base-agent 架构评审

本评审完全忽略 `bug.md`，只基于当前源码、模块依赖和架构文档。

## 总体判断

设计方向是对的——组合优于继承、Tool/Provider/Store 使用协议、资源按 Run 隔离、应用依赖核心——但项目已经从“小型运行时”发展成了一个职责交织的单体包。

最大问题不是代码量，而是 Run 生命周期的控制权分散在 Runtime、Strategy、Store、Agent 和 Server 之间。

## 架构级发现

### [P1] 把 Run 生命周期重新收归 Runtime

文档声称 Runtime 负责状态转换、持久化和事件，但 Strategy 当前可以直接：

- 修改 `RuntimeContext`
- 转换状态机
- 保存 Run 快照
- 写 EventStore

涉及位置：

- `src/base_agent/orchestration/model_tool.py:121`
- `src/base_agent/orchestration/planning.py:25`
- `src/base_agent/orchestration/react.py:48`

这导致自定义 Strategy 必须理解持久化顺序、事件语义和恢复规则，否则很容易产生“状态已改但未保存”“快照已保存但事件未发”等不一致。

建议引入统一的 `RunLifecycle` 或 `ExecutionJournal`：

```python
await lifecycle.record_model_response(response)
await lifecycle.record_tool_result(call, result)
await lifecycle.transition_to_waiting(pending_input)
await lifecycle.complete(output)
```

Strategy 只决定“下一步做什么”，不再直接访问 `RunStore`、`EventStore` 或 `save_context_snapshot()`。进一步可以让 `advance()` 返回类型化的 `AdvanceDecision`，由 Runtime 执行副作用。

这是最优先的重构。

### [P1] 为 Run 聚合建立事务边界

一个 Run 的一致状态目前分散在：

- `RunStore`
- `EventStore`
- `CheckpointStore`
- `ConversationStore`
- `ArtifactStore`

它们可以被完全独立地注入 `Agent`，而执行过程中又按顺序分别调用。

即使 `PostgresStore` 同时实现多个协议，Runtime 也没有事务抽象，因此“更新 Run + 追加事件 + 保存检查点 + 更新 Conversation Turn”不能作为一个提交单元。

建议增加 `RunUnitOfWork` 或更高层的 `RunRepository`：

```python
async with repository.transaction(run_id) as tx:
    await tx.update_snapshot(snapshot)
    await tx.append_events(events)
    await tx.save_checkpoint(checkpoint)
    await tx.finish_conversation_turn(...)
```

优先提供这些原子业务操作：

- `create_run`
- `record_transition`
- `suspend_run`
- `claim_resume`
- `finalize_run`

Redis 继续只做 commit 后通知，不参与事实存储。

### [P1] 修正实际依赖方向

声明的方向是“Runtime 依赖端口，适配器依赖核心”，但实际依赖图存在：

```text
runtime ↔ orchestration
runtime ↔ resources
runtime ↔ stores
runtime ↔ memory/artifacts
data_sources → providers
```

尤其 `RuntimeServices` 和 `ToolContext` 依赖的是具体 `ResourceManager`、`ArtifactManager`、`MemoryManager`，而不是窄协议。

建议目标分层：

```text
agent_app / composition
        ↓
application
  AgentFacade / RunCoordinator / LocalRunExecutor
        ↓
domain + ports
  models / transitions / provider-store-tool protocols
        ↑
adapters
  postgres / redis / openai / mcp / docker / playwright / mtbi
```

不必一次移动全部文件。先提取窄协议，例如：

- `ResourceAccess`
- `ArtifactAccess`
- `MemoryAccess`
- `RunLifecycle`
- `RunExecutor`

然后让 Runtime 和 Strategy 只依赖这些协议。

### [P2] 拆分 Agent 和 AgentRuntime 的组合职责

`Agent.__init__()` 已经接收近 20 项依赖；`AgentRuntime.execute()` 又接收十余项参数并现场构造 Manager。

当前 `Agent` 同时负责：

- 基础设施组合
- Skill/Attachment 解析
- Conversation 协调
- Run/Resume
- 后台 Task
- 日志初始化
- 查询 facade

建议引入两个不可变配置对象：

```python
@dataclass(frozen=True)
class AgentDefinition:
    profile: AgentProfile
    provider: ModelProvider
    tools: ToolRegistry
    skills: SkillRegistry
    resources: tuple[ResourceSpec, ...]


@dataclass(frozen=True)
class RuntimeDependencies:
    repository: RunRepository
    artifact_store: ArtifactStore
    supervisor: Supervisor
    executor: RunExecutor
```

`Agent` 保留用户友好的 facade；`RunCoordinator` 负责 run/resume；`LocalRunExecutor` 负责 `asyncio.Task`。不要引入重量级 DI 容器，继续由应用的 `build_agent()` 组合即可。

### [P2] 将 Strategy 状态从任意字典改为版本化状态

`RuntimeContext.supervision_data` 实际同时保存：

- ReAct iteration
- Planning phase
- suspended action batch
- Strategy 输出
- Supervisor 相关数据

这是一个跨 checkpoint 持久化的 `dict[str, Any]`，字段名称、版本和兼容规则都不受约束。

建议改成：

```python
class StrategyCheckpoint(BaseModel):
    kind: str
    version: int
    payload: dict[str, JsonValue]
```

并让 Strategy 提供：

```python
dump_state(context) -> StrategyCheckpoint
restore_state(checkpoint) -> StrategyState
```

同时把公开的 `RunStatus` 与内部执行阶段分开。`RUNNING` 不应同时代表 planning、executing、replanning、summarizing 等所有内部阶段。

### [P2] 为持久化模型增加 Schema Version

`RuntimeCheckpoint` 会直接保存完整 Profile、Skill、Message、ModelResponse、Memory 和任意 Strategy 数据，但没有：

- checkpoint schema version
- strategy version
- agent definition version/hash
- 升级器或兼容性错误

`Skill` 甚至包含本机 `Path`。跨机器恢复时该路径没有意义。

建议使用版本化 envelope：

```python
class CheckpointEnvelope:
    schema_version: int
    agent_definition: AgentDefinitionRef
    strategy: StrategyCheckpoint
    execution_state: ExecutionCheckpoint
```

需要明确选择一种策略：

- 固定恢复原 AgentDefinition 版本；或
- 允许升级，但必须经过显式兼容检查。

Run、Event 和 Checkpoint 都应有独立 schema version。

### [P2] 将 MTBI 等具体集成移出核心层

通用的 `ReadOnlyDataSource`、模型和 Tool bundle 属于核心扩展点；但 `src/base_agent/data_sources/mtbi_cli.py` 是具体公司平台集成，并通过根包直接导出。

这使 `data_sources` 反向依赖 `providers.cli`，也与“业务/厂商适配器依赖核心”的架构方向不一致。

建议保留：

```text
base_agent/data_sources/
  models.py
  protocol.py
  tools.py
```

将 MTBI 放到以下之一：

```text
agent_app/integrations/mtbi.py
base_agent/adapters/mtbi.py
独立 base-agent-mtbi 包
```

Docker、Playwright、Brave、OpenAI、PostgreSQL 等也建议逐步统一放入 `adapters` 命名空间。

### [P2] 缩小根包公共 API

当前 `base_agent/__init__.py` 有 146 个导出，已经与文档所说的“小型公共 API”不符。

根包同时暴露：

- 核心模型和协议
- Runtime 内部对象
- 具体 Provider
- 具体数据源
- Tool bundle
- Manager 和异常
- 测试/基础设施相关类型

建议根包只保留最常用的稳定入口：

```python
Agent
AgentProfile
AgentResult
ModelProvider
Tool
tool
SkillRegistry
Run
RunStatus
```

高级能力从稳定子包导入：

```python
from base_agent.runtime import ...
from base_agent.adapters.openai import ...
from base_agent.adapters.postgres import ...
from base_agent.data_sources import ...
```

通过 deprecation alias 渐进迁移，并增加公共 API snapshot 测试。

### [P2] 不要在库构造函数中配置文件日志

`Agent.__init__()` 会自动创建文件日志、修改 package logger 并关闭传播。

对一个可复用 library 来说，这是应用级副作用：

- 导入并构造 Agent 就写文件
- 应用无法自然接管 logging 配置
- 多 Agent/测试进程会共享全局 handler 状态

建议核心包只安装 `NullHandler`，提供显式的：

```python
base_agent.logging.configure_file_logging(...)
```

starter 可以默认调用它，但核心库不应自动配置。

## 建议保留的设计

以下方向是健康的，不建议推翻：

- Agent 通过组合构建，不继承 Runtime。
- Tool 使用类型注解生成 schema，并进行权限检查。
- Resource 按执行段获取和释放，WAITING 不序列化活对象。
- Artifact/Attachment 使用引用，不把二进制塞入消息。
- Provider、Store、Tool、Supervisor 基于 Protocol。
- Redis 只负责通知，持久 EventStore 才是事实来源。
- ReAct 和 Planning 作为 Strategy，而不是新的 Agent 子类。
- 离线 Provider 和 in-memory 实现保证确定性测试。

所以不需要重写项目；需要收紧生命周期和依赖边界。

## 推荐实施顺序

### 第一阶段：建立护栏

1. 增加 import-linter/依赖方向测试。
2. 增加公共 API snapshot。
3. 引入 `AgentDefinition`、`RuntimeDependencies`，保持现有构造函数兼容。
4. 将日志配置移到 starter。

### 第二阶段：集中生命周期

1. 引入 `RunLifecycle`。
2. Strategy 停止直接使用 Store 和 `save_context_snapshot()`。
3. 引入 `RunUnitOfWork`。
4. 统一 create/suspend/resume/finalize 的事务语义。

### 第三阶段：版本化恢复

1. 类型化 StrategyState。
2. 增加 Checkpoint/Event schema version。
3. 引入 AgentDefinitionRef 和兼容性检查。
4. 将内部 ExecutionPhase 与外部 RunStatus 分离。

### 第四阶段：整理扩展面

1. 移动 MTBI 等具体适配器。
2. 收缩根包导出。
3. 引入 `RunExecutor`，保留 `LocalRunExecutor` 默认实现。
4. 给 Tool 增加副作用、幂等性、取消和结果大小策略。

完成前两阶段后，项目的可靠性和可扩展性就会有明显提升；后两阶段主要解决跨版本恢复、插件增长和 1.0 公共 API 稳定性。
