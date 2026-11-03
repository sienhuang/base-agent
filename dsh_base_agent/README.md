# dsh-base-agent

`dsh_base_agent` 是公司级 Agent 应用平台和控制面，DSH 是底层 Agent 执行引擎。
它不重新实现 Agent Runtime。业务开发者只定义业务 Agent 及其能力；框架负责稳定的
Python SDK、业务 API、任务管理、租户权限、审计和生产治理，并把模型循环、Tool 调用、
Session、Skill、Workflow/Subagent 与上下文压缩交给 DSH。

## 分层

```text
业务项目
  ├── Context：本轮给模型什么信息
  ├── Memory：如何召回和保存长期信息
  ├── Tool：能执行哪些业务动作
  ├── Skill：怎样完成一类业务任务
  ├── Agent：选择哪些业务能力
  └── Flow：多个 Agent 怎样协作
             │
             ▼
       DshApplication
  ├── ProviderCatalog
  ├── Context/Tool/Skill/Agent/Flow Registry
  ├── Run / Event / SSE
  ├── FastAPI
  └── DSH Runtime lifecycle
             │
             ▼
      DeepSeek Harness
```

## 最小服务

Provider 默认从环境变量读取，业务代码不需要创建 DSH Runtime：

```python
from pathlib import Path

from dsh_base_agent import AgentDefinition, DshApplication

service = DshApplication(workspace=Path.cwd())

service.register_agent(
    AgentDefinition(
        id="assistant",
        version="1.0.0",
        instructions="请准确回答用户问题。",
    ),
    default=True,
)

api = service.fastapi()
```

应用只需要把 `api` 交给 Uvicorn。`DshApplication` 自动完成：

- LocalState 和 EventStore 创建。
- Provider 解析。
- DSH profile、Runtime 和 Session 管理。
- Run 后台执行、取消、事件回放和 SSE。
- FastAPI 启停时的 Runtime 清理。

## 完整业务接入

下面以订单业务为例。

### 1. 编写 Tool

```python
from dsh_base_agent import tool

@tool
async def query_order(order_id: str) -> dict[str, str]:
    """查询一个订单。"""
    return {"order_id": order_id, "status": "paid"}
```

### 2. 编写 Skill

```python
from dsh_base_agent import Skill, SkillDependencies

order_skill = Skill(
    name="order-service",
    description="订单处理规则",
    playbook="先查询订单，再根据真实订单状态回答。",
    dependencies=SkillDependencies(required_tools=("query_order",)),
)
```

### 3. 控制 Context 和 Memory

`Context` 是一等业务配置，组合 Policy、候选信息、MemoryScope、写回策略和 token 预算：

```python
from dsh_base_agent import (
    Context,
    ContextBlock,
    ContextSource,
    KeepAllContextPolicy,
    MemoryAugmentedContextPolicy,
    MemoryScope,
)

order_context = Context(
    name="orders",
    policy=MemoryAugmentedContextPolicy(
        KeepAllContextPolicy(),
        minimum_score=0.7,
    ),
    blocks=(
        ContextBlock(
            id="refund-policy",
            content="只有已支付订单才能申请退款。",
            source=ContextSource.APPLICATION,
            trusted=True,
            token_estimate=12,
        ),
    ),
    memory_scope=MemoryScope(("tenant-a", "orders")),
    memory_writeback=True,
    available_tokens=32_000,
)
```

业务可以实现 `Memory` Protocol 接入 MySQL、Redis 或向量库；开发阶段也可以使用
`InMemoryMemory`。

### 4. 注册业务 Agent

```python
from pathlib import Path

from dsh_base_agent import (
    AgentDefinition,
    DshApplication,
    InMemoryMemory,
)

service = DshApplication(
    workspace=Path.cwd(),
    memory=InMemoryMemory(),
)

service.register_context(order_context, default=True)
service.register_tool(query_order)
service.register_skill(order_skill)

order_agent = service.register_agent(
    AgentDefinition(
        id="order-agent",
        version="1.0.0",
        instructions="处理订单和售后问题。",
        skills=("order-service",),
    ),
    context="orders",
    default=True,
)
```

注册顺序有意保持明确：Context、Tool 和 Skill 是候选能力，`AgentDefinition` 决定
真正选择哪些能力。未选择的 Tool 不会暴露给 DSH。

### 5. 编排 Flow

```python
from dsh_base_agent import Flow, Stage

review_flow = Flow.sequence(
    id="order-review",
    stages=(
        Stage(name="handle", agent=order_agent),
        Stage(name="review", agent=review_agent, input_from="handle"),
    ),
    event_store=service.event_store,
)

service.register_flow(review_flow)
api = service.fastapi()
```

## Provider 配置

只有一个 Provider 时，框架默认读取：

```text
DSH_PROVIDER
DSH_MODEL
DSH_BASE_URL 或 DEEPSEEK_BASE_URL
DSH_API_KEY 或 DEEPSEEK_API_KEY
DSH_REASONING_EFFORT
DSH_MAX_TOKENS
```

多个 Provider 使用命名 profile：

```python
from dsh_base_agent import DshApplication, ProviderConfig

service = DshApplication(
    workspace=".",
    providers=(
        ProviderConfig.from_env(name="fast", prefix="FAST_DSH_"),
        ProviderConfig.from_env(name="quality", prefix="QUALITY_DSH_"),
    ),
    default_provider="fast",
)

service.register_agent(fast_agent_definition)
service.register_agent(quality_agent_definition, provider="quality")
```

API key 不会进入 Agent snapshot、Event 或对象 repr。

## HTTP 接口

```text
GET  /health/live
GET  /health/ready
POST /v1/runs
GET  /v1/runs/{run_id}
POST /v1/runs/{run_id}/cancel
GET  /v1/runs/{run_id}/events
GET  /v1/runs/{run_id}/events/stream
POST /v1/flows/{flow_id}
GET  /v1/flows/{flow_run_id}
GET  /v1/flows/{flow_run_id}/events
GET  /v1/flows/{flow_run_id}/events/stream
```

内置通用服务也可以直接启动：

```bash
uv run dsh-base-agent-server
```

## 验证

```bash
.venv/bin/ruff check src tests scripts
.venv/bin/mypy src
.venv/bin/pytest
.venv/bin/python scripts/preflight.py
uv build
```

当前版本适合本地开发、业务接入验证和受控测试环境。正式上线前仍需要生产级存储、
事件脱敏、认证授权、限流和多副本执行能力。
