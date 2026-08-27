# base-agent Starter — Planner-ReAct 集成

当前 Starter 在已经验收的 Prompt、Provider、Tool、Skill 和 Model Streaming 之上，接入了
SDK 的固定 Planner + ReAct Executor Flow，既可运行普通 Agent，也可直接测试 Plan 的
create → execute → review → complete/revise 生命周期。

> `SKILL.md` frontmatter 提供可自动发现的身份，正文总结可复用操作经验；
> 受信任 Skill 目录中的 `tools.py` 自动提供类型化 Tool，`scripts/` 默认作为 Tool
> 的私有实现。

Planner 只负责返回结构化 `PlannerDecision`，没有 Tool；Executor 复用 Starter 已有的
Tool、Skill、权限和确认策略，并返回结构化 `StepOutcome`。Plan、Step 状态和调度权仍由 Host
拥有，两个角色不会动态路由。

## 目录

```text
src/agent_app/
├── app.py                 # 唯一 composition root
├── config.py              # Provider 环境配置
├── provider.py            # Offline/API/CLI Provider 选择
├── cli.py                 # 命令行入口
├── asgi.py                # 只构造 Agent；HTTP 实现来自 base_agent.server
├── worker.py              # PostgreSQL Execution Worker composition root
├── execution_child.py     # Agent Handler 的隔离子进程协议
├── flow_execution_child.py # Flow Handler 的隔离子进程协议
├── assets.py              # Prompt asset loader
├── tools.py               # 应用拥有的类型化 Tools
├── skills/                # 应用拥有的 Skill catalog
│   ├── __init__.py
│   ├── value_recording/   # 一个 Skill 一个 package
│   │   ├── __init__.py
│   │   ├── SKILL.md       # Skill playbook
│   │   ├── tools.py       # 自动加载的 Skill Tools
│   │   └── scripts/
│   │       └── record.py  # Tool 背后的私有实现
│   └── python_calculator/ # Tool + Python Script Skill
│       ├── __init__.py
│       ├── SKILL.md
│       ├── tools.py       # calculate_sum/calculate_product Tools
│       └── scripts/
│           └── calculate.py
└── prompts/
    ├── assistant.md       # 普通 Agent Prompt
    ├── planner.md         # 固定 Planner Prompt
    └── executor.md        # 固定 ReAct Executor Prompt
```

`app.py` 只使用 SDK 公共门面；Starter 禁止导入 `base_agent.runtime`。

`skills/__init__.py` 只调用
`SkillCatalog.discover_trusted(Path(__file__).parent)`。它递归扫描 `SKILL.md`，
并自动导入每个 Skill 根下的 `tools.py`。因此新增普通 Skill 时不需要
`definition.py`，也不需要修改中央 `definitions=(...)` 列表。该 API 只能用于应用自己
拥有和审查的目录；外部 Skill 应使用不执行 Python 的 `SkillCatalog.discover()`。

## 安装

从仓库根目录执行：

```bash
uv sync --project next/starter --group dev
```

Starter 会固定读取 `next/starter/.env`，并让进程环境变量覆盖文件中的同名配置。默认模板
使用 OpenAI-compatible Provider：

```bash
cp next/starter/.env.example next/starter/.env
```

复制后在 `.env` 中填写实际的 `AGENT_MODEL`、`OPENAI_API_KEY`，使用兼容服务时再设置
`OPENAI_BASE_URL`。`.env` 被 Git 忽略，不应提交 API Key；`.env.example` 只包含可提交的
占位配置。若既没有 `.env` 也没有相关进程环境变量，Starter 才会回退到
OfflineProvider。

Starter 默认设置 `AGENT_STRUCTURED_OUTPUT_MODE=prompt`，适用于只兼容 Chat Completions、
但不支持 `response_format.type=json_schema` 的服务。Provider 会把 JSON Schema 写入系统指令，
Harness 仍会用 Pydantic 严格校验最终输出。原生支持严格响应格式的服务可以改为：

```dotenv
AGENT_STRUCTURED_OUTPUT_MODE=json-schema
```

只支持旧版 JSON mode 的服务也可以选择 `json-object`。

普通 Agent 使用 `AGENT_MODEL`。Planner-ReAct 默认让 Planner 和 Executor 继承这个模型；如果
需要按角色优化质量或成本，可以单独覆盖：

```dotenv
AGENT_MODEL=YOUR_DEFAULT_MODEL
PLAN_REACT_PLANNER_MODEL=YOUR_PLANNER_MODEL
PLAN_REACT_EXECUTOR_MODEL=YOUR_EXECUTOR_MODEL
```

Planner 模型负责创建和 Review Plan，需要可靠地产生结构化 `PlannerDecision`，但不调用 Tool；
Executor 模型负责多轮 ReAct、Tool Calling 和结构化 `StepOutcome`。CLI 也可使用
`--planner-model` 和 `--executor-model` 临时覆盖。未设置角色覆盖时会自动回退到 `--model`、
`AGENT_MODEL` 或 `OPENAI_MODEL` 选出的 Provider 默认模型。

如果两个角色不在同一个 OpenAI-compatible 端点，还可以分别配置完整的连接路由：

```dotenv
PLAN_REACT_PLANNER_BASE_URL=https://planner.example/v1
PLAN_REACT_PLANNER_API_KEY=YOUR_PLANNER_API_KEY
PLAN_REACT_PLANNER_STRUCTURED_OUTPUT_MODE=json-schema

PLAN_REACT_EXECUTOR_BASE_URL=https://executor.example/v1
PLAN_REACT_EXECUTOR_API_KEY=YOUR_EXECUTOR_API_KEY
PLAN_REACT_EXECUTOR_STRUCTURED_OUTPUT_MODE=prompt
```

每个未设置的角色字段都会继承对应的 `OPENAI_BASE_URL`、`OPENAI_API_KEY` 和
`AGENT_STRUCTURED_OUTPUT_MODE`。当两个角色解析出的连接配置完全相同时，Starter 会复用同一个
Provider；只要模型、Base URL、API Key 或结构化输出模式不同，就会构造独立 Provider。

`.env` 只配置 Provider 和基础设施，不定义 Agent 能力。普通 Tool、Skill、权限和执行预算统一
收敛在 `app.py` 的 `STARTER_AGENT_DEFINITION`；CLI 与 HTTP 都调用同一个 `build_agent()`，
不能分别改变能力白名单。

## 运行

```bash
uv run --project next/starter agent-app "hello"
```

输出为所配置模型的回复，例如：

```text
Hello! How can I help?
```

## 测试 Planner-ReAct

使用 `.env` 中现有的 OpenAI-compatible 配置运行真实模型：

```bash
uv run --project next/starter agent-app \
  --flow plan-react \
  --show-execution-trace \
  --show-tool-results \
  "计算 4 * 30"
```

Trace 会同时显示 Flow Event 和 Planner/Executor 子 Run Event。由于每个 Event stream 都有自己
的 sequence，Planner-ReAct 模式使用 `run_id` 前八位加 sequence 作为显示游标，例如
`[trace a1b2c3d4:5] step.started`。`flow.started` 给出 `plan_id`，每次
`planner.started`/`step.started` 给出对应 `agent_run_id`，因此可以核对所有子 Run 都属于同一次
Flow。

不依赖外部模型的确定性测试可显式选择 OfflineProvider，并要求 Executor 调用
`python-calculator` Skill 提供的 Tool：

```bash
uv run --project next/starter agent-app \
  --provider offline \
  --flow plan-react \
  --show-execution-trace \
  --show-tool-results \
  'tool=calculate_product calls=1 tool_args={"left":4,"right":30}'
```

预期可以看到 `calculate_product` 的 ToolResult 为 `{'product': 120}`，Flow 最终产生
`plan.completed` 和 `flow.succeeded`。OfflineProvider 现在声明并模拟严格结构化输出，因此这条
命令会经过与真实 Provider 相同的 `PlannerDecision`/`StepOutcome` 校验路径。

手动测试 Planner-ReAct 的动态 Artifact 选择：

```bash
uv run --project next/starter agent-app \
  --provider offline \
  --flow plan-react \
  --show-execution-trace \
  --show-tool-results \
  'tool=record_value calls=1 tool_args={"value":42}'
```

Trace 应先出现 `artifact.created artifact=recorded_value`，再出现
`artifact.selected artifact=recorded_value`。前者表示 Tool 创建了候选产物，后者表示 Executor
从 Tool observation 的 `artifacts_created` receipt 中选中了正式 Step 输出。只有 selected
Artifact 会进入 Flow 结果并授权给后续 Executor；单 Step 的 Review Planner 只接收元数据，
所以这里不应出现 `artifact.granted`。

Planner-ReAct 同样接受 `--stream`，它会让 Planner 和 Executor 的 Provider 都使用
streaming。由于两者返回的是内部结构化 JSON，分片只作为 `child.message.delta` Event 发布，
不会直接写到 stdout；CLI 最终只打印完整校验后的 Flow output。`--show-execution-trace` 可实时
查看 Flow 与子 Run 生命周期。HTTP `/v1/runs` 仍保持普通 Agent Run 语义；Planner-ReAct
使用独立的 `/v1/flows` API。

```bash
uv run --project next/starter agent-app \
  --flow plan-react \
  --stream \
  --show-execution-trace \
  "计算 4 * 30"
```

## 启动 FastAPI

Starter 的 `asgi.py` 只负责构造具体 Agent 与 Flow，再通过统一的 `AgentApplication` 生成
FastAPI；路由、schemas、preflight 生命周期和 Run/Flow registry 全部由
`base_agent.server` 提供。启动本地服务：

```bash
uv run --project next/starter agent-server --host 127.0.0.1 --port 8000
```

也可以直接使用 uvicorn：

```bash
uv run --project next/starter \
  uvicorn agent_app.asgi:app --host 127.0.0.1 --port 8000
```

### 使用 MCP Server 与 OAuth

安装 MCP extra，复制示例配置，并让 API、Worker 和 execution child 使用同一个状态目录：

```bash
uv sync --project next/starter --extra mcp
cp next/starter/mcp.example.yaml next/starter/mcp.yaml
```

```dotenv
AGENT_MCP_CONFIG_PATH=mcp.yaml
AGENT_MCP_STATE_ROOT=.mcp
```

部署前先同步所有启用 MCP Server 的 Tool manifest，再启动 API 和 Worker：

```bash
uv run --project next/starter agent-mcp-sync
uv run --project next/starter agent-server --port 8001
uv run --project next/starter execution-worker
```

`agent-mcp-sync` 会并发连接所有启用的 MCP Server，只有全部发现成功后才开始写入各自的
manifest。没有设置 `AGENT_MCP_CONFIG_PATH` 时，该命令输出
`No MCP configuration; nothing to sync.` 并以退出码 `0` 结束，因此不使用 MCP 的部署也可以
无条件保留同一套启动命令。明确配置了不存在或无效的文件、缺少 Bearer 环境变量、远端不可达
或任一发现失败时，命令以非零状态退出，API 和 Worker 不应继续启动。

无认证、固定 Header 和 Bearer Token 不需要人工授权；`agent-mcp-sync` 会直接完成认证、
`initialize` 和 `tools/list`。OAuth 已有可用 Token 或 refresh token 时也会非交互刷新并同步。
OAuth Server 首次使用或 refresh token 失效时，需要先启动 API control plane，再通过 Operator
凭据发起授权：

```bash
curl -sS -X POST \
  http://127.0.0.1:8001/v1/mcp/connections/remote_github/authorize \
  -H 'Authorization: Bearer replace-with-a-long-random-secret'
```

响应中的 `authorization_url` 需要在浏览器打开；第三方完成授权后会回调配置里的
`/v1/mcp/oauth/callback`。Token 和远端 Tool schema 随后写入 `AGENT_MCP_STATE_ROOT`。授权完成后
停止临时 API，执行 `agent-mcp-sync`，再按上面的固定顺序启动 API 与 Worker。当前版本在 Run
内保持 Tool snapshot 不变；远端 Tool 清单变化时重新同步，并让 API 与 Worker 从同一份新
manifest 启动。独立 execution child 只读取已保存 Token 并按需连接，不会在 Worker 中发起
交互式 OAuth。

本地 Token 文件使用原子写和 `0600` 权限，但没有加密；多主机生产环境应将 SDK 的
`OAuthTokenStorage` 替换为共享数据库或 Secret Manager。完整配置与安全边界见
[`../MCP.md`](../MCP.md)。

### 使用 PostgreSQL EventStore

默认的 `AGENT_EVENT_STORE=memory` 适合本地快速测试，进程退出后 Event 会消失。要让普通
Agent、Planner、Executor 和外层 Flow 的 Event 都写入同一个 PostgreSQL EventStore，先安装
可选依赖：

```bash
uv sync --project next/starter --extra postgres
```

然后在 `next/starter/.env` 中配置：

```dotenv
AGENT_PROVIDER=offline
AGENT_EVENT_STORE=postgres
AGENT_POSTGRES_DSN=postgresql://agent:password@127.0.0.1:5432/agent
AGENT_POSTGRES_SCHEMA=base_agent
AGENT_EVENT_POLL_INTERVAL_SECONDS=0.1
AGENT_RUN_LEASE_SECONDS=30
AGENT_RUN_HEARTBEAT_SECONDS=10
AGENT_RUNTIME_BUILD_ID=release-2026-08-20
```

`AGENT_POSTGRES_SCHEMA` 是 PostgreSQL namespace，不是另一份配置文件。FastAPI 启动时会在该
schema 中创建 `base_agent_event_sequences`、`base_agent_events`、
`base_agent_checkpoints`、`base_agent_runs`、`base_agent_flow_runs`、
`base_agent_flow_checkpoints`、`base_agent_execution_requests`、
`base_agent_execution_attempts`、`base_agent_recovery_decisions`、
`base_agent_artifact_receipts` 和 `base_agent_side_effect_evidence`，关闭时释放 Starter 自己创建的
连接池。DSN、schema
和轮询间隔由 Starter 读取；EventStore 注入普通 Agent、Planner、Executor 和 Flow，共享同一
Engine 的 CheckpointStore 注入所有 Agent；RunStore/FlowRunStore 分别作为顶层 Agent/Flow 的
状态权威。

`AGENT_RUN_LEASE_SECONDS` 是本地 Executor 对 `RUNNING` Run 的所有权期限，
`AGENT_RUN_HEARTBEAT_SECONDS` 必须更短。每次心跳都用 lease token 做 fencing；服务故障后，超过
租约的 `RUNNING` 会在下次启动或访问时转成 `BLOCKED` 并产生 `run.blocked` Event，第一版不会自动
重放模型或 Tool。`AGENT_RUNTIME_BUILD_ID` 建议填写镜像 digest、Git SHA 或发布号；未填写时使用
Agent snapshot fingerprint。RunRecord 同时保存两者，但当前等待恢复仍要求 snapshot 完全一致。

### 使用独立 Execution Worker

本地模式仍是默认值。要让 FastAPI 只提交 Agent Run/Flow Run，并由独立 Supervisor 执行，在
API 和 Worker 使用的同一份 `.env` 中增加：

```dotenv
AGENT_EVENT_STORE=postgres
AGENT_EXECUTION_MODE=postgres-worker
AGENT_RUNTIME_ROUTE=starter-v1
AGENT_RUNTIME_BUILD_ID=release-2026-08-20
AGENT_RUN_LEASE_SECONDS=30
AGENT_RUN_HEARTBEAT_SECONDS=10
AGENT_WORKER_POLL_SECONDS=0.5
AGENT_WORKER_RECONCILE_SECONDS=5
AGENT_WORKER_MAX_CONCURRENCY=10
```

分别启动 API 和 Worker。Operator token 只提供给 API，Worker 启动和 child 环境不需要该权限：

```bash
AGENT_OPERATOR_TOKEN=replace-with-a-long-random-secret \
  uv run --project next/starter --extra postgres \
  agent-server --host 127.0.0.1 --port 8000

uv run --project next/starter --extra postgres execution-worker
```

`agent-worker` 作为兼容入口仍可使用。`POST /v1/runs` 和 `POST /v1/flows/plan-react` 此时都返回
`queued`。同一个 Worker 从同一张 request 表领取任务，再按 `target_kind=agent_run|flow_run`
分派给 Agent 或 Flow Handler；每个 Attempt 启动隔离 child。`AGENT_WORKER_MAX_CONCURRENCY` 默认
为 10，Worker 只按空闲槽位 claim，并为每个活动 Attempt 独立维护 heartbeat、取消和结算；Core
直接组合 `ExecutionWorker` 时仍默认 1，避免改变已有部署容量。Supervisor 在 child 外续租，所以
任一 child 内同步阻塞的 Tool 不会阻塞其他 Attempt 的 heartbeat。Worker shutdown 会停止 claim、
终止全部活动 child，并将它们分别转为 `recovery_required(reason=worker_shutdown)`。child Event 会携带 `execution_attempt_id`，
PostgreSQL EventStore 在同一事务校验 fencing token 后才接受 Event。

同一事务还会把 `artifact.created` 投影为 receipt，并把 `tool.started/waiting/completed` 投影为
side-effect evidence。Tool operation ID/idempotency key 跨 Attempt 稳定，intent hash 防止同一
operation 被不同参数复用；旧 fencing token 不能写 Event、receipt 或 evidence。FlowCheckpoint
只验证 Host 已接受或选择且 metadata 一致的 Artifact。

调用方重试 Agent 或 Flow 提交时应复用 `Idempotency-Key` 请求头；相同 key 和相同输入返回同一个
逻辑任务，相同 key 但不同输入返回 `409`。

Worker 正常停止会把活动 Agent/Flow 转成 `recovery_required(reason=worker_shutdown)`；异常退出则由任一
Worker 的周期 reconciler 在 lease 真正过期后转成
`recovery_required(reason=owner_lease_expired)`，不会自动重跑。恢复上下文直接查询原接口：

```bash
curl http://127.0.0.1:8000/v1/runs/RUN_ID

curl http://127.0.0.1:8000/v1/operator/runs/RUN_ID/recovery-decisions \
  -H 'Authorization: Bearer replace-with-a-long-random-secret' \
  -H 'Content-Type: application/json' \
  -d '{
    "decision":"retry",
    "strategy":"restart",
    "expected_revision":12,
    "subject_id":"operator-001",
    "reason_code":"verified_safe",
    "idempotency_key":"ticket-123-retry-1"
}'
```

`GET` 响应的 `recovery` 会显示 `allowed_strategies`、`artifact_receipts`、
`side_effect_evidence` 和 `strategy_blockers`。read-only 或 intent 一致的 idempotent 操作可以重放；
未被目标 checkpoint 覆盖的 mutating/unknown 操作会从允许集合中移除。即使 Operator 提交该策略，
Store 也会在创建新 ExecutionRequest 前返回 `409`，所以 `reason_code` 不能绕过门禁。

`resume`/`confirm` 仍只用于 `waiting`，不能替代 Operator recovery decision。第一版独立 Worker
已经覆盖顶层 Agent Run 和 `plan-react` Flow：Flow 在 Plan/Step 安全边界写入 FlowCheckpoint，
WAITING 时同时保存父 Flow cursor 与等待中的 child Run ID。Operator 可通过
`/v1/operator/flows/{flow_run_id}/recovery-decisions` 选择 `from_checkpoint` 或 `restart`。
`from_checkpoint` 只能复用该 checkpoint 已验证的 Artifact，并可跳过其覆盖的副作用；`restart`
检查整个 Flow 的证据，不会绕过较早的未知副作用。
Starter 的 `LocalArtifactStore` 仍要求 API、Worker child 能看到同一路径；多节点生产部署应换成
共享 ArtifactStore。

启动服务并分别提交普通 Run 和 Planner-ReAct Flow：

```bash
uv run --project next/starter --extra postgres \
  agent-server --host 127.0.0.1 --port 8000

curl http://127.0.0.1:8000/v1/runs \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"hello"}'

curl http://127.0.0.1:8000/v1/flows/plan-react \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"计算 4 * 30"}'
```

用返回的 `run_id` 或 `flow_run_id` 查询数据库；每个 ID 都拥有独立且从 1 开始的 sequence：

```sql
SELECT sequence, kind, turn, payload
FROM base_agent.base_agent_events
WHERE run_id = '<run_id-or-flow_run_id>'
ORDER BY sequence;

SELECT run_id, status, revision, lease_owner, lease_expires_at,
       runtime_build_id, snapshot_fingerprint, updated_at
FROM base_agent.base_agent_runs
WHERE run_id = '<agent-run-id>';

SELECT flow_run_id, flow_name, flow_kind, status, revision,
       checkpoint_revision, current_attempt_id, updated_at
FROM base_agent.base_agent_flow_runs
WHERE flow_run_id = '<flow-run-id>';
```

也可以运行真实 PostgreSQL 集成测试。测试会经 FastAPI 创建一个普通 Run 和一个
Planner-ReAct Flow，关闭第一个应用连接池，再创建一个 Registry 为空的新 FastAPI 应用，验证
状态接口、历史 JSON、终态 SSE、外层 Flow 及三个子 Run，最后删除测试专用 schema：

```bash
BASE_AGENT_NEXT_TEST_POSTGRES_DSN='postgresql://agent:password@127.0.0.1:5432/agent' \
  uv run --project next/starter --group dev \
  pytest tests/test_postgres_events.py -q
```

本地模式的 Registry 仍只保留当前进程 Handle；queued 模式下普通 Agent 与 Flow 分别由
PostgreSQL RunStore/FlowRunStore 管理状态、取消请求和结果，EventStore 负责历史与 SSE。
服务重启后，已完成和 `WAITING` 的 Agent/Flow 都可以直接查询；`WAITING` 分别凭 Agent checkpoint
与 Flow checkpoint 在新 child 中继续。故障遗留的 `RUNNING` 在租约到期后进入
`RECOVERY_REQUIRED`，由人工检查副作用后决定 `restart`/`from_checkpoint`/取消/失败，不会自动
拉起。把 Run ID 用在 Flow 路由（或反过来）仍返回 404。

提交并查询 Run：

```bash
curl http://127.0.0.1:8000/v1/runs \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"hello"}'

curl http://127.0.0.1:8000/v1/runs/<run_id>
```

取消仍在运行的 Run；重复取消已取消的 Run 是幂等的，已经成功或失败的 Run 返回 `409`：

```bash
curl -X POST http://127.0.0.1:8000/v1/runs/<run_id>/cancel
```

### 手动测试 WAITING 与确认

Starter FastAPI 显式使用 `AwaitHumanConfirmations`。用 OfflineProvider 请求需要确认的
`save_value`：

```bash
curl http://127.0.0.1:8000/v1/runs \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"tool=save_value calls=1 tool_args={\"value\":42}"}'
```

查询返回的 `run_id`，状态应为 `waiting`，并包含 `pending_input.request_id`：

```bash
curl http://127.0.0.1:8000/v1/runs/<run_id>
```

批准精确请求。`subject_id` 应由认证层提供；这里仅用于本地手测：

```bash
curl -X POST http://127.0.0.1:8000/v1/runs/<run_id>/confirm \
  -H 'Content-Type: application/json' \
  -d '{
    "request_id":"<pending-request-id>",
    "decision":"approve",
    "subject_id":"manual-user",
    "reason_code":"manual_test"
  }'
```

拒绝时把 `decision` 改成 `reject`；Harness 会为原 ToolCall 生成
`confirmation_denied` Tool result，再让模型完成回答。

Starter 也显式启用了基础 `ask_user` Tool 和 `interaction:ask` 权限。真实 Provider 会根据
Prompt 和 Tool 描述自行判断是否调用；下面的 OfflineProvider 语法用于确定性地触发同一链路：

```bash
curl http://127.0.0.1:8000/v1/runs \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"tool=ask_user calls=1 tool_args={\"question\":\"Which region?\",\"metadata\":{\"field\":\"region\"}}"}'
```

取得返回的 `run_id` 后，查询结果应为 `waiting`，`pending_input.tool_name` 为 `ask_user`。
把用户回答提交给原 Run：

```bash
curl -X POST http://127.0.0.1:8000/v1/runs/<run_id>/resume \
  -H 'Content-Type: application/json' \
  -d '{"input":"APAC"}'
```

`run.waiting` 是一次 SSE 边界；confirm/resume 后从它的 sequence 重新连接，即可观察
`run.resumed` 到下一次 waiting 或终态。

回放 sequence 大于 3 的历史 Event：

```bash
curl 'http://127.0.0.1:8000/v1/runs/<run_id>/events?after_sequence=3'
```

从头订阅 SSE；它会先补历史、再实时跟随，Run 结束后自动关闭：

```bash
curl -N -H 'Accept: text/event-stream' \
  http://127.0.0.1:8000/v1/runs/<run_id>/events/stream
```

断线后可用最后收到的 SSE `id` 续传：

```bash
curl -N -H 'Last-Event-ID: 3' \
  http://127.0.0.1:8000/v1/runs/<run_id>/events/stream
```

空闲 SSE 默认每 15 秒发送 comment 心跳。心跳不写入 EventStore，也不改变事件游标。

### 通过 FastAPI 测试 Planner-ReAct

Starter 在组装 Planner-ReAct 时会让 Planner 和 Executor 共享同一个
`LocalArtifactStore`，因此 Executor 选中的 Artifact 可以经 Flow 授权给后续 Executor Run。
Store 默认位于 `next/starter/.artifacts`，可通过 `AGENT_ARTIFACT_ROOT` 修改。
`ArtifactManager` 仍然按 Run 隔离：共享的是 Store，不是 Run 的访问权限。

提交 Flow，接口立即返回 `202` 和 `flow_run_id`：

```bash
curl http://127.0.0.1:8000/v1/flows/plan-react \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"计算 4 * 30","stream":true}'
```

查询状态与最终 Plan：

```bash
curl http://127.0.0.1:8000/v1/flows/<flow_run_id>
```

从头订阅 Flow SSE：

```bash
curl -N -H 'Accept: text/event-stream' \
  http://127.0.0.1:8000/v1/flows/<flow_run_id>/events/stream
```

也可以回放、续传或取消：

```bash
curl 'http://127.0.0.1:8000/v1/flows/<flow_run_id>/events?after_sequence=3'

curl -N -H 'Last-Event-ID: 3' \
  http://127.0.0.1:8000/v1/flows/<flow_run_id>/events/stream

curl -X POST http://127.0.0.1:8000/v1/flows/<flow_run_id>/cancel
```

Flow SSE 除了 `plan.created`、`step.completed` 等外层 Event，还直接使用普通 Agent 的
`model.*`、`skill.*`、`tool.*` 和 `context.*` kind。原 payload 字段保持不变，并追加
`event_scope=child_run`、`child_role`、`flow_phase`、`agent_run_id`、`child_sequence` 等关联信息；
内部 Run 和 Message 使用 `child.run.*`、`child.message.*`，避免误判 Flow 终态或最终消息。
Flow 原始请求与最终结果分别使用标准 `message.created`、`message.completed`，随后发送
`flow.succeeded` 并关闭 SSE。所有事件仍以 Flow 自己的 sequence 作为唯一续传游标。
请求中的 `stream=true` 只控制子 Agent 是否调用 Provider streaming；无论该值如何，Event
历史和 SSE 路由始终可用。不支持 streaming 的任一角色 Provider 会使 Flow 明确进入
`flow.failed`，不会静默回退为完整响应。

HTTP 服务和 CLI 使用同一份 `.env` Provider 配置。模板选择 `openai-compatible`，需要配置
`AGENT_MODEL`、`OPENAI_API_KEY`，连接非 OpenAI 服务时再设置 `OPENAI_BASE_URL`；Agent 能力
始终来自同一份 `STARTER_AGENT_DEFINITION`。

## 测试 Model Streaming

OfflineProvider 会把完整文本拆成确定性的 `ModelTextDelta`。Starter 使用 `--stream` 实时
消费 Harness 的 `message.delta` Event：

```bash
uv run --project next/starter agent-app --stream "hello"
```

最终显示内容仍然是：

```text
Starter Agent received: hello
```

但输出是在 Run 完成前逐段写入。`Agent.stream()` 结束后仍返回完整 `RunResult`，所以事件、
ToolResult、验证证据和最终文本不会因为 streaming 丢失。

流式 ToolCall 也必须先完整聚合参数再执行：

```bash
uv run --project next/starter agent-app \
  --stream \
  --show-tool-results \
  'tool=record_value calls=1'
```

预期最终包含：

```text
executed=1, not_found=0, denied=0, confirmation_denied=0, invalid_arguments=0, error=0, timeout=0, limit_exceeded=0
call-1: status=success, data=1
```

Harness 对文本发布 `message.delta`；对 ToolCall 分片只发布 index、ToolCall ID、Tool 名称
和参数字节数，原始未校验参数不会进入 Event。收到完整 `ModelStreamCompleted` 后，才进入
原有 Tool allowlist、权限、参数、确认、超时和执行链。

## 测试 Skill

### Skill 自带 Tool

`STARTER_AGENT_DEFINITION` 选择 `value-recording`；Agent 因此自动加入 Skill 贡献的
`record_value`，应用不再直接注册它。Tool 调用 Skill 包内的 `scripts/record.py`。受信任
Loader 自动收集 `tools.py` 中的 Tool，Package Loader 同时把 script 记入包清单；Definition
显式授予 `values:record`，而不是从 Tool 声明反向推导权限。

验证 Skill 提供的 Tool、流式 ToolCall 和最终结果：

```bash
uv run --project next/starter agent-app \
  --stream \
  --show-tool-results \
  'tool=record_value calls=1'
```

预期最终包含：

```text
executed=1, not_found=0, denied=0, confirmation_denied=0, invalid_arguments=0, error=0, timeout=0, limit_exceeded=0
call-1: status=success, data=1
```

自动化测试还会验证 snapshot 包含 `value-recording`、包指纹、自动加载的 Tool
和 scripts 清单。System Prompt 只包含轻量 Skill Descriptor；完整 playbook 由模型调用
`skill_view` 后作为 ToolResult 进入上下文。模型可见 Tool 列表包含 `skill_view` 和
`record_value`。

先单独测试按需加载 playbook：

```bash
uv run --project next/starter agent-app \
  --show-execution-trace \
  --show-tool-results \
  'tool=skill_view calls=1 tool_args={"name":"value-recording"}'
```

Trace 中应先出现 `skill.selected`，成功执行 `skill_view` 后才出现
`skill.loaded ... file=SKILL.md`。ToolResult 的 `data.content` 是完整 playbook，并包含可继续
读取的 `linked_files` 清单。

### Tool 调用 Package Script

`python-calculator` 的 `tools.py` 声明 `calculate_sum` 和 `calculate_product`，它们内部导入
`scripts/calculate.py`。模型只看到类型化 Tool，不选择文件路径，也不直接操作子进程。

`STARTER_AGENT_DEFINITION` 同时选择 `python-calculator` 并显式授予
`skills:scripts:execute`。验证执行：

```bash
uv run --project next/starter agent-app \
  --show-execution-trace \
  --show-tool-results \
  'tool=calculate_sum calls=1 tool_args={"left":2,"right":3}'
```

预期最终包含：

```text
executed=1, not_found=0, denied=0, confirmation_denied=0, invalid_arguments=0, error=0, timeout=0, limit_exceeded=0
[trace 2] skill.selected skill=python-calculator version=1.0.0
[trace 6] skill.resource.used skill=python-calculator type=tool resource=calculate_sum tool=calculate_sum call=call-1
[trace 7] tool.started tool=calculate_sum call=call-1
[trace 8] tool.completed tool=calculate_sum call=call-1 status=success
call-1: status=success, data={'sum': 5}
```

`--show-execution-trace` 按 Event sequence 展示 Run、Skill、模型和 Tool 链路。这里只出现
`skill.selected`，因为离线测试直接指定 `calculate_sum`，并未读取 playbook；
`skill.resource.used` 表示 Harness 已接受执行该 Skill 自有资源。模型调用 `skill_view` 后才
会产生 `skill.loaded`。Trace 通过单次 Run 的 `on_event` 实时写入 stderr，
不等待 Run 结束后遍历 `RunResult.events`；模型正文和 ToolResult 写入 stdout。真实 Provider
生成的 ToolCall ID 不保证是 `call-1`。

Skill 贡献的 Tool 不需要再写入 `AgentDefinition.tools`；Definition 只选择 Skill，Agent
根据 Catalog 自动加入对应 Tool。缺少权限、未选择 Skill 等反例通过复制 Definition 的
单元测试验证，不给 CLI 或 HTTP 增加修改生产能力策略的入口。

该 Tool 通过普通 Python import 复用 Package Script 的函数。如果未来确实需要模型直接
选择 Script 路径，再单独使用 SDK 的高级 `SkillScriptExecution` 适配器。

可以修改 `src/agent_app/prompts/assistant.md` 后再次运行。测试会确认 Prompt 被固定到本次
Run 的 `HarnessSnapshot` 中。

## 测试 Toolkit

Toolkit 只负责在应用构造阶段组合相关 Tool：

```python
DIAGNOSTIC_TOOLKIT = Toolkit.of(
    "diagnostics",
    fail_operation,
    slow_operation,
)

INTERACTION_TOOLS = interaction_tools()
REGISTERED_TOOLS = (*INTERACTION_TOOLS, DIAGNOSTIC_TOOLKIT, save_value, browse)
ENABLED_TOOLS = (*INTERACTION_TOOLS, DIAGNOSTIC_TOOLKIT, save_value)
ENABLED_TOOL_NAMES = tuple(
    tool.spec.name for tool in flatten_tools(ENABLED_TOOLS)
)
```

Harness 会把 Toolkit 展开成 `fail_operation` 和 `slow_operation`。模型看到和调用的仍然是
具体 Tool 名称，不会看到名为 `diagnostics` 的 Tool。`record_value` 和 calculator Tool 由
所选 Skill 自动贡献，不放入普通 `REGISTERED_TOOLS`。可以复用异常命令验证 Toolkit 中的
Tool 能正常执行：

```bash
uv run --project next/starter agent-app \
  --show-tool-results \
  'tool=fail_operation calls=1 tool_args={"message":"expected failure"}'
```

预期仍然返回 `call-1: status=error`。SDK 同时拒绝空白 Toolkit 名称、Toolkit 内部重名，
以及 Toolkit 与其他注册 Tool 之间的重名。

## 测试 Observation 基础契约

每个 ToolResult 都会转换成一条发送给 Provider 的 Tool Message：

```json
{
  "status": "success",
  "data": 1,
  "error": null,
  "artifacts_created": [
    {
      "artifact_id": "<uuid>",
      "name": "recorded_value",
      "media_type": "application/json",
      "size_bytes": 11,
      "sha256": "<sha256>",
      "source_artifact_ids": []
    }
  ]
}
```

Message 的 `tool_call_id` 和 `name` 分别关联原 ToolCall ID 与具体 Tool。使用
OfflineProvider 的确定性测试标记可以显示 Provider 实际收到的 Observation：

```bash
uv run --project next/starter agent-app \
  'tool=record_value calls=2 show_observations=true'
```

预期每条 observation 都包含 `status`、`data`、`error` 和该次 ToolCall 创建的
`artifacts_created` receipt；两条记录的 ToolCall ID 依次为 `call-1`、`call-2`。
这里验证 JSON 字段、Tool 名称、ToolCall ID 和顺序。`show_observations=true` 只属于
OfflineProvider 的确定性测试输入，不进入 Harness，也不适用于真实 Provider。

## 测试 Tool Readiness

Starter 直接实现了一个可读的 readiness 回调：

```python
def check_browser_readiness() -> ToolReadiness:
    executable = os.getenv("STARTER_BROWSER_EXECUTABLE", "").strip()
    if not executable:
        return ToolReadiness(
            ready=False,
            detail="STARTER_BROWSER_EXECUTABLE is not configured",
        )
    if shutil.which(executable) is None:
        return ToolReadiness(
            ready=False,
            detail=f"browser executable '{executable}' was not found",
        )
    return ToolReadiness(ready=True)


@tool(readiness=check_browser_readiness)
async def browse(url: str) -> str:
    return f"Browser is ready for {url}"
```

`browse` 位于 `REGISTERED_TOOLS`，但不在 `ENABLED_TOOLS`，因此不会影响 CLI 或 HTTP。
readiness 的失败与成功分支通过复制 `STARTER_AGENT_DEFINITION`、仅在单元测试中加入
`browse` 来验证。依赖缺失时会在调用模型之前失败：

```text
error: Tool 'browse' is not ready: browser executable 'definitely-missing-starter-browser' was not found
```

Harness 会产生 `tool.not_ready` 事件；不会调用 Provider、不会执行 Tool，也不会产生
ToolResult。测试就绪分支时，将 `STARTER_BROWSER_EXECUTABLE` 指向真实存在且可执行的
浏览器程序即可。

## 测试已注册但未启用的 Tool

`browse` 已经在应用中注册，但默认不在 `AgentDefinition.tools` 白名单。OfflineProvider
可以故意模拟模型请求这个未启用 Tool：

```bash
uv run --project next/starter agent-app \
  --show-tool-results \
  'tool=browse calls=1 tool_args={"url":"https://example.com"}'
```

预期：

```text
executed=0, not_found=1, denied=0, confirmation_denied=0, invalid_arguments=0, error=0, timeout=0, limit_exceeded=0
call-1: status=not_found, data=None, error="Tool 'browse' is not enabled for this Agent"
```

该调用不会执行 `check_browser_readiness` 或 `browse`，不会产生 `tool.not_ready` 或
`tool.started`；Harness 只产生 `tool.completed(status=not_found)`，并将该结果作为
Observation 交给最后一个 Model Turn。

## 测试 Tool Permission

`record_value` 声明了精确字符串权限：

```python
@tool(permissions=frozenset({"values:record"}))
async def record_value(value: int) -> int:
    return value
```

`STARTER_AGENT_DEFINITION` 显式授予 `values:record`，因此 CLI 与 HTTP 都可以执行：

```bash
uv run --project next/starter agent-app \
  --show-tool-results \
  "Call record_value once. calls=1"
```

预期：

```text
executed=1, not_found=0, denied=0, confirmation_denied=0, invalid_arguments=0, error=0, timeout=0, limit_exceeded=0
call-1: status=success, data=1
```

权限会进入 `HarnessSnapshot`，但不会进入 Prompt 或发送给 Provider。未授权拒绝路径通过
单元测试复制 Definition 并清空权限来验证；CLI 和 HTTP 不提供临时扩权或降权入口。

## 测试 Tool 参数校验

`@tool` 根据函数签名生成 JSON Schema。`record_value(value: int)` 要求 `value` 是必填
integer，并拒绝额外字段。

通过 Prompt 明确传入错误类型：

```bash
uv run --project next/starter agent-app \
  --show-tool-results \
  'Call record_value once. calls=1 tool_args={"value":"abc"}'
```

预期：

```text
executed=0, not_found=0, denied=0, confirmation_denied=0, invalid_arguments=1, error=0, timeout=0, limit_exceeded=0
call-1: status=invalid_arguments, data=None, error='...valid integer...'
```

还可以分别测试缺少必填字段和额外字段：

```bash
# 缺少 value
uv run --project next/starter agent-app \
  --show-tool-results \
  'Call record_value once. calls=1 tool_args={}'

# 存在额外字段
uv run --project next/starter agent-app \
  --show-tool-results \
  'Call record_value once. calls=1 tool_args={"value":1,"extra":true}'
```

三种错误都必须在参数验证阶段返回 `invalid_arguments`，且不产生 `tool.started` 事件。
`tool_args={...}` 是 OfflineProvider 的确定性测试输入；使用真实 Provider 时可以在自然
语言 Prompt 中要求模型故意提供错误参数，但模型可能根据 Tool Schema 主动修正。

## Runtime Context 边界

普通 `@tool` 函数只声明业务参数，不接收 `ToolContext`：

```python
@tool
async def record_value(value: int) -> int:
    return value
```

`run_id`、`agent_id`、`tool_call_id` 和内部幂等标识由 Harness 创建并保留在
`Harness → Tool.invoke()` 的执行边界，用于事件、治理和后续 Runtime 能力。它们不进入
模型 Schema，也不注入开发者的 FunctionTool 函数。`@tool` 函数声明 `ToolContext`
会在应用构造阶段直接得到 `TypeError`。

## 测试 Tool 执行异常

`fail_operation` 在参数校验通过后主动抛出受控异常：

```python
@tool
async def fail_operation(message: str) -> str:
    raise RuntimeError(message)
```

仍然通过 `agent-app` 和 Prompt 选择这个 Tool：

```bash
uv run --project next/starter agent-app \
  --show-tool-results \
  'tool=fail_operation calls=1 tool_args={"message":"expected failure"}'
```

预期：

```text
executed=0, not_found=0, denied=0, confirmation_denied=0, invalid_arguments=0, error=1, timeout=0, limit_exceeded=0
call-1: status=error, data=None, error='RuntimeError: expected failure'
```

该场景必须依次产生 `tool.started` 和 `tool.completed(status=error)`。异常不能逃出
Harness；Error ToolResult 会作为 Observation 进入最后一个 Model Turn。

`tool=<name>`、`calls=N` 和 `tool_args={...}` 是 OfflineProvider 用于稳定驱动 Starter
主路径的测试输入格式，后续 Tool 场景复用同一格式。

## 测试 Tool 执行超时

`slow_operation` 的最大执行时间是 0.05 秒：

```python
@tool(timeout_seconds=0.05)
async def slow_operation(delay_seconds: float) -> str:
    await asyncio.sleep(delay_seconds)
    return f"completed after {delay_seconds:g}s"
```

通过 `agent-app` 要求它等待 0.2 秒：

```bash
uv run --project next/starter agent-app \
  --show-tool-results \
  'tool=slow_operation calls=1 tool_args={"delay_seconds":0.2}'
```

预期：

```text
executed=0, not_found=0, denied=0, confirmation_denied=0, invalid_arguments=0, error=0, timeout=1, limit_exceeded=0
call-1: status=timeout, data=None, error='Tool exceeded 0.05s timeout'
```

该场景必须依次产生 `tool.started` 和 `tool.completed(status=timeout)`。Harness 负责取消
超时执行，并将 Timeout ToolResult 作为 Observation 交给最后一个 Model Turn；Tool
超时不应作为未捕获异常逃出本次 Run。

## 测试副作用确认

`save_value` 同时声明写权限、可变副作用和必需确认：

```python
@tool(
    permissions=frozenset({"values:write"}),
    side_effect=SideEffect.MUTATING,
    confirmation=Confirmation.REQUIRED,
)
async def save_value(value: int) -> int:
    return value
```

Definition 已授予 `values:write`，但默认 ConfirmationPolicy 仍拒绝本次副作用：

```bash
uv run --project next/starter agent-app \
  --show-tool-results \
  'tool=save_value calls=1 tool_args={"value":7}'
```

预期 Tool 不会真正开始：

```text
executed=0, not_found=0, denied=0, confirmation_denied=1, invalid_arguments=0, error=0, timeout=0, limit_exceeded=0
call-1: status=confirmation_denied, data=None, error='Tool execution was not confirmed'
```

允许确认的分支通过向 `build_agent()` 注入测试 ConfirmationPolicy 验证。CLI 和当前 HTTP
Adapter 不提供“全部批准”开关；生产应用应接入经过身份认证、针对单次 ToolCall 作出决定并
保留审计记录的 ConfirmationPolicy。

## 测试每轮 ToolCall 上限

仍然使用 Starter 的主入口 `agent-app`。下面的 Prompt 要求 Provider 在同一个 Model Turn
返回 4 个 `record_value` ToolCall，而统一 Definition 每轮只允许执行前 3 个：

```bash
uv run --project next/starter agent-app \
  --show-tool-results \
  "Call record_value once per value in the same model turn. calls=4"
```

离线 Provider 会读取 Prompt 中明确的 `calls=4`，因此该命令能够稳定验收 Harness；
但整个调用仍经过正常主路径：

```text
agent-app
  → build_agent()
  → AgentDefinition + record_value
  → Harness
  → OfflineProvider
  → 4 个 ToolCall
  → 每轮上限 3
```

预期输出：

```text
executed=3, not_found=0, denied=0, confirmation_denied=0, invalid_arguments=0, error=0, timeout=0, limit_exceeded=1
call-1: status=success, data=1
call-2: status=success, data=2
call-3: status=success, data=3
call-4: status=limit_exceeded, data=None, error='...max_tool_calls_per_turn=3'
```

使用真实 Codex CLI Provider 时，Prompt 直接要求模型一次返回 4 个 ToolCall：

```bash
uv run --project next/starter agent-app \
  --provider codex-cli \
  --show-tool-results \
  "In one model turn, call record_value exactly 4 times with values 1, 2, 3, and 4. Do not answer directly before calling all 4 Tools."
```

`prompts/assistant.md` 也包含“同一轮返回全部 `record_value` 调用”的系统指令，可以直接
修改这个 Prompt 后重新运行。真实模型具有一定非确定性；应先用 offline 命令验收
Harness，再用 API/CLI Provider 验收模型是否遵循 Prompt。

对应的 Agent 配置是：

```python
AgentDefinition(
    ...,
    max_model_turns=10,
    max_tool_calls_per_turn=3,
)
```

模型停止调用 Tool 后生成总结；如果耗尽十个回合，最后一个 Model Turn 固定禁用 Tool。

## 测试 OpenAI-compatible Provider

先设置凭据。密钥从 `next/starter/.env` 或进程环境读取，不通过 CLI 参数传递；进程环境优先：

```bash
export OPENAI_API_KEY="your-api-key"
```

连接 OpenAI 默认地址时，不需要设置 `base-url`：

```bash
uv run --project next/starter agent-app \
  --provider openai-compatible \
  --model YOUR_MODEL_NAME \
  --stream \
  "请用一句话介绍你自己"
```

连接其他实现了 OpenAI Chat Completions 协议的服务时，指定它的 API 根地址。该地址
通常需要包含 `/v1`：

```bash
export OPENAI_API_KEY="not-needed"

uv run --project next/starter agent-app \
  --provider openai-compatible \
  --base-url http://127.0.0.1:8000/v1 \
  --model YOUR_SERVED_MODEL_NAME \
  "请用一句话介绍你自己"
```

也可以使用环境变量，避免每次输入地址和模型：

```bash
export OPENAI_BASE_URL="http://127.0.0.1:8000/v1"
export OPENAI_MODEL="YOUR_SERVED_MODEL_NAME"

uv run --project next/starter agent-app \
  --provider openai-compatible \
  "hello"
```

本步骤调用的是 `OpenAIChatProvider`，使用 Chat Completions 协议。省略 `--stream` 时使用
完整回复；添加 `--stream` 时使用上游 `stream=True` 并由 Harness 聚合最终响应。

## 测试本地 CLI Provider

CLI Provider 复用了旧版 base-agent 的设计：把本机已经安装并完成登录的 Codex CLI 或
Claude CLI 作为 `ModelProvider`。它不需要 `OPENAI_API_KEY`。

使用 Codex CLI 的默认模型：

```bash
uv run --project next/starter agent-app \
  --provider codex-cli \
  "请只回复：codex cli provider works"
```

显式选择 Codex 模型：

```bash
uv run --project next/starter agent-app \
  --provider codex-cli \
  --model YOUR_CODEX_MODEL \
  "hello"
```

使用 Claude CLI 的默认模型：

```bash
uv run --project next/starter agent-app \
  --provider claude-cli \
  "请只回复：claude cli provider works"
```

也可以完全通过环境变量选择：

```bash
export AGENT_PROVIDER="codex-cli"
export AGENT_CLI_TIMEOUT_SECONDS="300"
export AGENT_CLI_MAX_OUTPUT_BYTES="4194304"

uv run --project next/starter agent-app "hello"
```

CLI Provider 可使用的配置：

| 环境变量 | CLI 参数 | 作用 |
|---|---|---|
| `AGENT_PROVIDER` | `--provider` | `offline`、`openai-compatible`、`codex-cli` 或 `claude-cli` |
| `AGENT_MODEL` | `--model` | 可选模型；CLI Provider 省略时使用本地 CLI 默认模型 |
| `AGENT_CLI_EXECUTABLE` | `--cli-executable` | 覆盖 `codex` 或 `claude` 命令路径 |
| `AGENT_CLI_TIMEOUT_SECONDS` | — | 子进程超时，默认 300 秒 |
| `AGENT_CLI_MAX_OUTPUT_BYTES` | — | stdout/stderr 单流上限，默认 4 MiB |

SDK 不通过 shell 拼接命令。Codex 运行在临时工作目录、ephemeral 会话和 read-only
sandbox 中；Claude 的内置 Tools、slash commands 和会话持久化均被关闭。两者的
stdout 都会进行结构化解析，并受到超时和输出大小限制。

CLI Provider 是使用 Agent CLI 模拟模型边界的本地开发便利模式，不等同于生产环境中的
原生模型 API。生产部署仍建议使用明确认证、限流和可观测的 API Provider。
当前 Codex/Claude CLI Adapter 只返回完整结构化响应，不支持 `--stream`；Starter 会在
启动子进程前返回明确的 configuration error。

## 自动验收

```bash
uv run --project next/starter pytest \
  -c next/starter/pyproject.toml next/starter/tests
uv run --project next/starter ruff check \
  next/starter/src next/starter/tests
uv run --project next/starter mypy \
  --config-file next/starter/pyproject.toml next/starter/src/agent_app
```

## 人工验收点

1. CLI 能返回预期文本；
2. 修改 Prompt 不需要修改 Runtime；
3. `app.py` 不构造 Store、Harness、Worker 或 Flow；
4. 测试可注入 `FakeModel`，不需要网络；
5. RunResult 包含固定的 agent id、model name、system Prompt 和 fingerprint。
6. Toolkit 展开为具体 Tool；Toolkit 名称不进入模型可见的 Tool Schema。
7. 空 Toolkit 名称和任何跨边界的 Tool 重名都会在构造阶段失败。
8. Observation 固定包含 `status`、`data`、`error`，并保持 ToolCall ID、名称和顺序。
9. 未启用 `browse` 时不检查其依赖；显式启用但依赖缺失时在模型调用前失败。
10. 模型请求未启用的 `browse` 时返回 `not_found`，且不检查 readiness。
11. `agent-app` 主路径只执行前 N 个 ToolCall；
12. 超限 ToolCall 具有对应的 `limit_exceeded` ToolResult 和 Observation；
13. 最终 Model Turn 禁用 Tool 并消费前一轮结果。
14. 唯一 `STARTER_AGENT_DEFINITION` 显式选择 Tool、Skill 和权限；CLI/HTTP 不再二次拼装；
15. 单元测试复制 Definition 并移除 `values:record` 后，Tool 返回 `denied` 且不产生
    `tool.started`。
16. `fail_operation` 抛出的异常被转换为 `error` ToolResult，产生
    `tool.started`/`tool.completed` 事件，且异常不会逃出 Harness。
17. `slow_operation` 超时后返回 `timeout` ToolResult，产生
    `tool.started`/`tool.completed` 事件，且 Run 可以继续完成。
18. `save_value` 有权限但未确认时返回 `confirmation_denied`，且不产生
    `tool.started`。
19. 对本次运行显式批准副作用后，`save_value` 才返回 `success`。
20. `--stream` 在 Run 完成前发布文本 delta，结束后仍返回完整 RunResult。
21. 流式 ToolCall 必须完整聚合参数后才进入 Tool 治理与执行链。
22. 未选择 `value-recording` Skill 时，snapshot 不包含 Skill playbook。
23. 选择 Skill 但缺少 `values:record` 时，`record_value` 返回 `denied`，其他无需
    该权限的 Skill 能力仍可用。
24. `discover_trusted()` 自动加载两个 Skill 的 `tools.py`，无 `definition.py` 或中央
    definitions 列表；playbook、Tool、包指纹与 scripts 清单可审计。

这些项目通过后，再继续增加下一个可独立验收的 SDK 能力。
