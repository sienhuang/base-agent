# base-agent Starter — Step 03: Skill 组合

当前 Starter 在已经验收的 Prompt、Provider、Tool 和 Model Streaming 之上增加一个
应用拥有的 Skill Package：

> `SKILL.md` frontmatter 提供可自动发现的身份，正文总结可复用操作经验；
> 受信任 Skill 目录中的 `tools.py` 自动提供类型化 Tool，`scripts/` 默认作为 Tool
> 的私有实现。

本阶段没有 Flow、持久化或恢复；已支持 Provider 到 CLI 的模型文本 streaming，并通过一个
薄 ASGI composition root 使用 SDK 提供的 HTTP Run Server。

## 目录

```text
src/agent_app/
├── app.py                 # 唯一 composition root
├── config.py              # Provider 环境配置
├── provider.py            # Offline/API/CLI Provider 选择
├── cli.py                 # 命令行入口
├── asgi.py                # 只构造 Agent；HTTP 实现来自 base_agent.server
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
    └── assistant.md       # 开发者首先修改的文件
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

## 运行

```bash
uv run --project next/starter agent-app "hello"
```

输出为所配置模型的回复，例如：

```text
Hello! How can I help?
```

## 启动 FastAPI

Starter 的 `asgi.py` 只负责构造具体 Agent，路由、schemas 和 Run registry 全部由
`base_agent.server` 提供。启动本地服务：

```bash
uv run --project next/starter agent-server --host 127.0.0.1 --port 8000
```

也可以直接使用 uvicorn：

```bash
uv run --project next/starter \
  uvicorn agent_app.asgi:app --host 127.0.0.1 --port 8000
```

提交并查询 Run：

```bash
curl http://127.0.0.1:8000/v1/runs \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"hello"}'

curl http://127.0.0.1:8000/v1/runs/<run_id>
```

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

HTTP 服务和 CLI 使用同一份 `.env` 配置。模板选择 `openai-compatible`，需要配置
`AGENT_MODEL`、`OPENAI_API_KEY`，连接非 OpenAI 服务时再设置 `OPENAI_BASE_URL`。

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
  --grant-permission values:record \
  --max-model-turns 2 \
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

Starter 默认仍直接注册 `record_value`，以保留 Step 02 的测试。增加
`--enable-recording-skill` 后，应用不再注册这个 Tool，改由 `value-recording` Skill
自动贡献；Tool 调用 Skill 包内的 `scripts/record.py`，同时 Skill 组合自己的
playbook。受信任 Loader 自动收集 `tools.py` 中的 `record_value`，Package Loader
同时把 `scripts/record.py` 记入包清单。`values:record` 由 Tool 自己声明。

先验证未授权时具体 ToolCall 被 Harness 拒绝：

```bash
uv run --project next/starter agent-app \
  --enable-recording-skill \
  --max-model-turns 2 \
  --show-tool-results \
  'tool=record_value calls=1 tool_args={"value":1}'
```

预期输出：

```text
call-1: status=denied, data=None, error='missing permissions: values:record'
```

再授予权限，验证 Skill 提供的 Tool、流式 ToolCall 和最终结果：

```bash
uv run --project next/starter agent-app \
  --enable-recording-skill \
  --grant-permission values:record \
  --max-model-turns 2 \
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
  --enable-recording-skill \
  --grant-permission values:record \
  --max-model-turns 2 \
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

先验证缺少执行权限时 ToolCall 返回 `denied`：

```bash
uv run --project next/starter agent-app \
  --enable-python-script-skill \
  --max-model-turns 2 \
  --show-tool-results \
  'tool=calculate_sum calls=1 tool_args={"left":2,"right":3}'
```

再授权执行：

```bash
uv run --project next/starter agent-app \
  --enable-python-script-skill \
  --grant-permission skills:scripts:execute \
  --max-model-turns 2 \
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

Agent(
    ...,
    tools=(
        record_value,
        DIAGNOSTIC_TOOLKIT,
        save_value,
        browse,
    ),
)
```

Harness 会把 Toolkit 展开成 `fail_operation` 和 `slow_operation`。模型看到和调用的仍然是
具体 Tool 名称，不会看到名为 `diagnostics` 的 Tool。可以复用异常命令验证 Toolkit 中的
Tool 能正常执行：

```bash
uv run --project next/starter agent-app \
  --max-model-turns 2 \
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
  "error": null
}
```

Message 的 `tool_call_id` 和 `name` 分别关联原 ToolCall ID 与具体 Tool。使用
OfflineProvider 的确定性测试标记可以显示 Provider 实际收到的 Observation：

```bash
uv run --project next/starter agent-app \
  --grant-permission values:record \
  --max-model-turns 2 \
  'tool=record_value calls=2 show_observations=true'
```

预期：

```json
{"observations":[{"content":{"data":1,"error":null,"status":"success"},"name":"record_value","tool_call_id":"call-1"},{"content":{"data":2,"error":null,"status":"success"},"name":"record_value","tool_call_id":"call-2"}]}
```

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

`browse` 默认不在 Agent Profile 中启用，因此不会影响普通 Starter 运行。使用
`--enable-browser-tool` 才会把它加入本次 Agent 的已启用 Tool：

```bash
STARTER_BROWSER_EXECUTABLE=definitely-missing-starter-browser \
uv run --project next/starter agent-app \
  --enable-browser-tool \
  "test browser readiness"
```

预期在调用模型之前失败：

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
  --max-model-turns 2 \
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

不授予权限时：

```bash
uv run --project next/starter agent-app \
  --max-model-turns 2 \
  --show-tool-results \
  "Call record_value once. calls=1"
```

预期 Tool 不会开始执行：

```text
executed=0, not_found=0, denied=1, confirmation_denied=0, invalid_arguments=0, error=0, timeout=0, limit_exceeded=0
call-1: status=denied, data=None, error='missing permissions: values:record'
```

显式授予权限：

```bash
uv run --project next/starter agent-app \
  --grant-permission values:record \
  --max-model-turns 2 \
  --show-tool-results \
  "Call record_value once. calls=1"
```

预期：

```text
executed=1, not_found=0, denied=0, confirmation_denied=0, invalid_arguments=0, error=0, timeout=0, limit_exceeded=0
call-1: status=success, data=1
```

`--grant-permission` 可以重复使用。权限会进入 `AgentDefinition.permissions` 和本次
`HarnessSnapshot`；它不会进入 Prompt，也不会传给 Provider。

## 测试 Tool 参数校验

`@tool` 根据函数签名生成 JSON Schema。`record_value(value: int)` 要求 `value` 是必填
integer，并拒绝额外字段。

通过 Prompt 明确传入错误类型：

```bash
uv run --project next/starter agent-app \
  --grant-permission values:record \
  --max-model-turns 2 \
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
  --grant-permission values:record \
  --max-model-turns 2 \
  --show-tool-results \
  'Call record_value once. calls=1 tool_args={}'

# 存在额外字段
uv run --project next/starter agent-app \
  --grant-permission values:record \
  --max-model-turns 2 \
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
  --max-model-turns 2 \
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
  --max-model-turns 2 \
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

先授予权限，但不确认本次副作用：

```bash
uv run --project next/starter agent-app \
  --grant-permission values:write \
  --max-model-turns 2 \
  --show-tool-results \
  'tool=save_value calls=1 tool_args={"value":7}'
```

预期 Tool 不会真正开始：

```text
executed=0, not_found=0, denied=0, confirmation_denied=1, invalid_arguments=0, error=0, timeout=0, limit_exceeded=0
call-1: status=confirmation_denied, data=None, error='Tool execution was not confirmed'
```

再对本次演示运行显式批准副作用：

```bash
uv run --project next/starter agent-app \
  --grant-permission values:write \
  --approve-side-effects \
  --max-model-turns 2 \
  --show-tool-results \
  'tool=save_value calls=1 tool_args={"value":7}'
```

预期：

```text
executed=1, not_found=0, denied=0, confirmation_denied=0, invalid_arguments=0, error=0, timeout=0, limit_exceeded=0
call-1: status=success, data=7
```

`--grant-permission` 表示 Agent 有资格调用；`--approve-side-effects` 表示本次演示运行明确
同意执行副作用。默认 ConfirmationPolicy 是拒绝，确认服务异常时同样 fail-closed。
`--approve-side-effects` 使用允许全部确认的演示策略；生产应用应换成经过身份认证、针对
单次 ToolCall 作出决定并保留审计记录的 ConfirmationPolicy。

## 测试每轮 ToolCall 上限

仍然使用 Starter 的主入口 `agent-app`。下面的 Prompt 要求 Provider 在同一个 Model Turn
返回 3 个 `record_value` ToolCall，而 Harness 只允许执行前 2 个：

```bash
uv run --project next/starter agent-app \
  --grant-permission values:record \
  --max-model-turns 2 \
  --max-tool-calls-per-turn 2 \
  --show-tool-results \
  "Call record_value once per value in the same model turn. calls=3"
```

离线 Provider 会读取 Prompt 中明确的 `calls=3`，因此该命令能够稳定验收 Harness；
但整个调用仍经过正常主路径：

```text
agent-app
  → build_agent()
  → AgentDefinition + record_value
  → Harness
  → OfflineProvider
  → 3 个 ToolCall
  → 每轮上限 2
```

预期输出：

```text
executed=2, not_found=0, denied=0, confirmation_denied=0, invalid_arguments=0, error=0, timeout=0, limit_exceeded=1
call-1: status=success, data=1
call-2: status=success, data=2
call-3: status=limit_exceeded, data=None, error='...max_tool_calls_per_turn=2'
```

使用真实 Codex CLI Provider 时，Prompt 直接要求模型一次返回 3 个 ToolCall：

```bash
uv run --project next/starter agent-app \
  --provider codex-cli \
  --grant-permission values:record \
  --max-model-turns 2 \
  --max-tool-calls-per-turn 2 \
  --show-tool-results \
  "In the same model turn, call record_value exactly 3 times with values 1, 2, and 3. Do not answer directly before calling all 3 Tools."
```

`prompts/assistant.md` 也包含“同一轮返回全部 `record_value` 调用”的系统指令，可以直接
修改这个 Prompt 后重新运行。真实模型具有一定非确定性；应先用 offline 命令验收
Harness，再用 API/CLI Provider 验收模型是否遵循 Prompt。

对应的 Agent 配置是：

```python
AgentDefinition(
    ...,
    tools=("record_value",),
    max_model_turns=2,
    max_tool_calls_per_turn=2,
)
```

第一个 Model Turn 申请和执行 Tool；第二个也是最后一个 Model Turn 禁用 Tool，只根据全部
Observation 生成总结。

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
14. 未授予 `values:record` 时 Tool 返回 `denied` 且不产生 `tool.started`；
15. `--grant-permission values:record` 后同一个 ToolCall 才返回 `success`。
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
