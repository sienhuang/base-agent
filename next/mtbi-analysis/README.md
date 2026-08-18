# MTBI Analysis

这是一个独立的 `base-agent-next` 下游应用。它不导入 Starter，也不把 MTBI 领域逻辑放入
SDK。Starter 仅作为项目配置和 Provider 组合方式的参考。

## 执行结构

```text
用户请求
  → MTBI Query Agent
  → Host 校验 SQL 与 CSV
  → Data Analysis Agent
  → Host 校验 Markdown 报告
```

Query Agent 只能调用只读配置/认证检查、元数据和 OneSQL 命令。所有 SQL、CSV 和 Markdown
必须写到配置的 workspace 内；绝对路径、目录穿越、超限文件、无表头或无数据行 CSV 都会被
Host 拒绝。第一个 Stage 验证失败时，第二个 Agent 不会启动。
Query Stage 最多尝试三次，Analysis Stage 最多尝试两次。只有 Agent Run 已成功、但 Host
verifier 拒绝产物时才会重试；验证证据会进入下一次独立 Agent Run。

## 配置

```bash
cd next/mtbi-analysis
cp .env.example .env
```

默认使用 Codex CLI，不需要在这个应用的 `.env` 中保存模型 API 凭据：

```dotenv
AGENT_PROVIDER=codex-cli
AGENT_CLI_EXECUTABLE=codex
AGENT_CLI_TIMEOUT_SECONDS=300
AGENT_CLI_MAX_OUTPUT_BYTES=4194304
AGENT_MAX_TOOL_OBSERVATION_BYTES=32768
AGENT_MAX_RUN_OBSERVATION_BYTES=524288
MTBI_STRUCTURED_OUTPUT_MODE=prompt
```

省略 `AGENT_MODEL` 时使用本地 Codex CLI 的默认模型；需要固定模型时设置
`AGENT_MODEL=...`。运行前先确认 `codex --version` 和本地登录状态正常。

其中 `AGENT_CLI_MAX_OUTPUT_BYTES` 限制 Codex CLI 进程输出；另外两个 observation 配置限制
应用 Tool 返回给模型的单次大小和整个 Run 的累计大小。MTBI metadata 通常比普通 Tool 输出
更大，因此这里使用 32 KiB/512 KiB；最终解析值会进入 HarnessSnapshot 和 fingerprint。

如需切换 OpenAI-compatible Provider，再设置：

```dotenv
AGENT_PROVIDER=openai-compatible
OPENAI_API_KEY=YOUR_API_KEY
OPENAI_BASE_URL=https://your-openai-compatible-endpoint.example/v1
OPENAI_MODEL=YOUR_MODEL
```

应用默认从 `PATH` 查找 `mtbi-cli`。也可以设置绝对路径：

```dotenv
MTBI_CLI_BIN=/absolute/path/to/mtbi-cli
```

启动前先独立确认 MTBI 环境：

```bash
mtbi-cli auth status
mtbi-cli config show
```

应用也会在启动 Codex 前执行同样的 Host preflight。即使 `auth status` 的退出码为 0，只要输出
显示 token 已过期，应用仍会立即停止并提示对应的登录命令，例如：

```bash
mtbi-cli auth login --project 12
```

## 运行

从仓库根目录执行：

```bash
uv run --project next/mtbi-analysis mtbi-analysis \
  "查询最近一天的登录用户数据，并分析用户活跃情况"
```

也可以临时覆盖非凭据配置：

```bash
uv run --project next/mtbi-analysis mtbi-analysis \
  --model YOUR_MODEL \
  --base-url https://your-endpoint.example/v1 \
  --workspace /tmp/mtbi-analysis-workspace \
  "查询最近一天的登录用户数据，并分析用户活跃情况"
```

成功后 CLI 会打印 SQL、CSV、数据校验摘要和报告路径。每次执行使用独立目录：

```text
workspace/runs/<flow-run-id>/
├── query.sql
├── dataset.csv
└── report.md
```

这三个位置由 Host 声明为 `ArtifactSpec`，模型只使用它们调用 Tool，不再负责在结构化输出中
复述路径。Tool 写入后会把同一份字节提交到 `workspace/.artifacts` 下的
`LocalArtifactStore`；SQL、CSV 和报告都带 SHA-256、producer Run/Stage/attempt 和 Tool call
来源，报告还记录源 CSV Artifact ID。workspace 是运行 Resource，不再被当作持久 Artifact
本身。CLI 打印 Host 校验后的绝对路径。

## FastAPI 与完整 Run Event Stream

从仓库根目录启动：

```bash
uv run --project next/mtbi-analysis mtbi-analysis-server \
  --host 127.0.0.1 \
  --port 8000
```

启动完整 MTBI Flow：

```bash
curl -sS -X POST http://127.0.0.1:8000/v1/flows/mtbi-analysis \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"查询最近一天的登录用户数据，并分析用户活跃情况","stream":false}'
```

响应中的 `flow_run_id` 可用于回放或实时订阅。使用 Codex CLI Provider 时保持
`stream=false`；该字段控制模型文本 delta，与 SSE Event 订阅相互独立。

```bash
curl -N http://127.0.0.1:8000/v1/flows/FLOW_RUN_ID/events/stream
curl -sS http://127.0.0.1:8000/v1/flows/FLOW_RUN_ID/events
curl -sS http://127.0.0.1:8000/v1/flows/FLOW_RUN_ID
```

Flow Event Stream 同时包含 `flow.*`、`stage.*`，以及转发后的 `child.run.*`、
`child.message.*`、`model.*`、`tool.*` 等子 Run 事件。每个子 Run Event 的 payload
都带 `event_scope=child_run`，并包含 `stage`、`agent_run_id`、`child_kind` 和
`child_sequence`，可区分 query 与 analysis。

这是纯 Flow 服务，不注册 `/v1/runs`。`GET /v1/flows/{flow_run_id}` 的 `artifacts` 字段返回
已验证 SQL、CSV 和报告的不可变引用及 hash/provenance；发生验证重试时，`child_runs` 会列出同一 Stage 的多次
attempt，Event Stream 会出现 `stage.verification_failed` 和 `stage.retrying`。

## 测试

自动测试不访问真实 Provider 或 MTBI 服务。`FakeModel` 仍会走完整 Agent/Tool 循环，假的
MTBI runner 负责生成确定性 CSV：

```bash
cd next/mtbi-analysis
uv sync --group dev
uv run pytest
uv run ruff check src tests
uv run mypy src
```

## 当前边界

- 当前 FastAPI Registry 和 EventStore 均为进程内实现，不支持多 Worker 或跨 Pod 查询。
- `mtbi_cli` 不允许 `config set` 等配置修改命令。
- CSV 分析使用有界行数、文件大小、样本和数值摘要；不是无限制的数据处理引擎。
- 文件存在并不代表成功，最终以 Host verifier 的确定性检查为准。
- 当前使用单机 `LocalArtifactStore`；跨 Pod 元数据、对象存储、租户授权、保留策略和垃圾回收
  仍需要生产 Adapter。
