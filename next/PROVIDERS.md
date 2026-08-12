# Provider — 当前验收基线

Provider 是 Harness 与模型服务之间的窄腰边界。它只负责：

1. 声明 Provider 身份、默认模型和可映射能力；
2. 将 provider-neutral `ModelRequest` 转换为上游请求；
3. 将上游响应转换为统一 `ModelResponse`；
4. 将上游异常转换为统一 `ProviderError`。

重试、fallback、Run 生命周期、Tool 执行和上下文管理不属于 Provider Adapter。

## 1. 核心契约

```python
from collections.abc import AsyncIterator

from base_agent.models import (
    ModelRequest,
    ModelResponse,
    ModelStreamEvent,
)


class ModelProvider(Protocol):
    @property
    def info(self) -> ProviderInfo: ...

    async def complete(self, request: ModelRequest) -> ModelResponse: ...


class StreamingModelProvider(ModelProvider, Protocol):
    def stream(
        self,
        request: ModelRequest,
    ) -> AsyncIterator[ModelStreamEvent]: ...
```

`ProviderInfo` 包含：

- `name`：Adapter/服务身份，例如 `openai-compatible`；
- `default_model`：Agent 未指定模型时使用的 model id；
- `capabilities`：当前支持 Tool、结构化输出和 streaming 与否。

模型解析规则：

```text
AgentDefinition.model
        ↓ 存在则使用
ProviderInfo.default_model
        ↓ 两者都不存在则构造 Agent 失败
ModelRequest.model
        ↓
HarnessSnapshot.model_name
```

Provider 名称和最终 model id 分别进入 `HarnessSnapshot`，不能混成同一个字符串。

## 2. 标准化响应

`ModelResponse` 当前包含：

- `content`；
- provider-neutral `ToolCall`；
- `FinishReason`；
- `TokenUsage`；
- 有界扩展字段 `provider_data`。

`TokenUsage` 区分 input、output、cached input 和 reasoning token。通用字段进入顶层，
协议特有的 request id、served model 和 system fingerprint 留在 `provider_data`。

## 3. 能力检查

Provider 必须显式声明：

```python
ProviderCapabilities(
    tools=True,
    structured_output=True,
    streaming=True,
)
```

如果 Agent 启用了 Tool，而 Provider 不支持 Tool，`Agent(...)` 构造立即失败；结构化输出
同理。不能等模型请求发出后才发现协议不支持。

声明 `streaming=True` 的 Provider 必须实现 `stream()`。Harness 会拒绝 capability 与实现
不一致的 Provider，也不会用一次性 `complete()` 冒充 streaming。

流事件分为：

- `ModelTextDelta`：立即发布模型文本；
- `ModelToolCallDelta`：按 index 携带 ToolCall id、名称和参数片段；
- `ModelStreamCompleted`：携带聚合后的完整 `ModelResponse`。

Harness 只在收到 `ModelStreamCompleted` 后处理 ToolCall。参数片段尚未聚合、解析和校验
完成时绝不执行 Tool。文本 delta 同时转换成有序的 `message.delta` Event；ToolCall
delta 只发布不含原始参数的元数据 Event，避免未治理参数通过观察面泄漏。

Delta 是实时但暂定的输出：连接可能在部分文本后失败。消费者必须以随后出现的
`message.completed` 取得完整 assistant 消息，并以 `run.succeeded`、`run.failed` 或
`run.cancelled` 判断最终状态，不能把收到第一个 delta 等同于 Run 成功。Harness 还会校验
已发布文本与 completed response 一致。

## 4. 错误分类

| 错误 | retryable |
| --- | --- |
| `ProviderAuthenticationError` | 否 |
| `ProviderInvalidRequestError` | 否 |
| `ProviderRateLimitError` | 是 |
| `ProviderTimeoutError` | 是 |
| `ProviderUnavailableError` | 是 |
| `InvalidProviderResponseError` | 否 |

Harness 不应把这些错误压成无法判断的字符串。失败 Run 同时提供：

```python
result.error
result.failure.code
result.failure.retryable
result.failure.details
```

## 5. OpenAI-compatible Adapter

安装可选 SDK：

```bash
uv sync --project next --extra openai
```

构造 Adapter：

```python
from base_agent.providers import OpenAIChatProvider

provider = OpenAIChatProvider(
    default_model="gpt-4.1-mini",
    api_key="...",
    base_url=None,
)
```

Adapter 使用 Chat Completions 协议，当前支持：

- system/user/assistant/tool message 转换；
- Tool schema 和 tool choice；
- JSON Schema structured output；
- token usage；
- ToolCall 参数 JSON 解析；
- text delta streaming；
- 按 ToolCall index 聚合分片参数；
- streaming 完成响应、finish reason、usage 和 metadata 归一化；
- 常见 HTTP/SDK 错误分类。

Adapter 内部关闭 SDK 自动重试。统一重试策略应由后续 Harness policy 提供，避免 SDK、
Adapter 和 Harness 多层叠加重试。

## 6. 本地 CLI Provider

开发和 SDK 验收时，可以让本机已经完成登录的 Codex CLI 或 Claude CLI 实现
`ModelProvider`：

```python
from base_agent import Agent, AgentDefinition
from base_agent.providers import CodexCLIProvider

provider = CodexCLIProvider()
agent = Agent(
    AgentDefinition(
        id="cli-assistant",
        version="1.0.0",
        instructions="Answer briefly.",
    ),
    model=provider,
)
result = await agent.run("hello")
```

省略 `model` 时使用本地 CLI 的默认模型。Harness 快照使用 `codex-cli-default` 或
`claude-cli-default` 表示这一选择；显式传入模型时，快照记录具体模型名称。

```python
from base_agent.providers import ClaudeCLIProvider

provider = ClaudeCLIProvider(
    model="YOUR_MODEL",
    executable="claude",
    timeout_seconds=300,
    max_output_bytes=4 * 1024 * 1024,
)
```

CLI Provider 继承了旧版 base-agent 的执行约束：

- 使用 argv 启动子进程，不调用 shell；
- 每次请求使用独立临时目录；
- 设置执行超时及 stdout/stderr 大小上限；
- Codex 使用 ephemeral、read-only sandbox 和结构化输出；
- Claude 禁用内置 Tools、slash commands 和会话持久化；
- CLI 输出必须通过 schema、Tool 名称和参数 JSON 校验后才能进入 Harness。

这是本地开发便利模式，并不是生产环境原生模型 API 的替代品。
CLI Provider 当前只提供完整响应，`ProviderCapabilities.streaming` 保持 `False`。

## 7. 自定义 Provider

```python
from base_agent.models import ModelRequest, ModelResponse
from base_agent.providers import ProviderCapabilities, ProviderInfo


class MyProvider:
    @property
    def info(self) -> ProviderInfo:
        return ProviderInfo(
            name="my-provider",
            default_model="my-model",
            capabilities=ProviderCapabilities(),
        )

    async def complete(self, request: ModelRequest) -> ModelResponse:
        return ModelResponse(content="hello")
```

应用负责从环境或 Secret 管理系统读取凭据，再把 Provider 注入 `Agent`。Secret 不进入
AgentDefinition、Prompt、Run metadata 或 snapshot。

## 8. 当前不包含

- HTTP SSE transport、事件持久化与断线重放；
- reasoning/audio/image delta；
- 自动重试和退避；
- fallback/model routing；
- Responses API、Anthropic 和 Gemini Adapter；
- Provider credential refresh；
- Prompt cache accounting policy。

这些能力需要分别验收，不能一次塞入 Provider 基线。

## 9. 验收

```bash
.venv/bin/pytest -c next/pyproject.toml next/tests/test_providers.py
```

测试完全离线，通过注入假的 OpenAI client 检查实际请求映射、响应归一化、错误分类和
Harness failure 传播。
