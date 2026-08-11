# base-agent next

`next/` 是按 Agent SDK 产品目标重新实现的独立内核，不是旧 Runtime 的目录搬迁。

开发者主路径只有：

```text
Prompt + Tool + Skill
        ↓
 AgentDefinition + Agent
        ↓
   Harness（自动运行、观察、验证、治理）
        ↓
 RunResult
```

简单多 Agent 应用在此之上使用声明式 `SequentialFlow`，不直接接触生命周期仓库、
Invoker、Worker 或恢复状态机。

## 最小示例

```python
from base_agent import Agent, AgentDefinition
from base_agent.testing import FakeModel

agent = Agent(
    AgentDefinition(
        id="assistant",
        version="1.0.0",
        instructions="Answer clearly and briefly.",
    ),
    model=FakeModel.text("Hello."),
)

result = await agent.run("Hi")
print(result.output)
```

普通 `Agent.run()` 可以通过一次性的 `on_event` 实时观察 Harness Event；这与 Provider
是否支持模型输出 streaming 相互独立：

```python
def on_event(event):
    print(event.sequence, event.kind)

result = await agent.run("Hi", on_event=on_event)
```

支持 streaming 的 Provider 使用 `Agent.stream()`，同一个 Observer 还会收到
`model.output.delta`，结束后仍返回完整 `RunResult`：

```python
def on_event(event):
    if event.kind == "model.output.delta":
        print(event.payload["text"], end="", flush=True)

result = await agent.stream("Hi", on_event=on_event)
```

Tool、Skill 和 Flow 的完整离线示例见 `examples/quickstart.py`。
Skill Package 的目录、Tool 贡献、Script 执行边界和 snapshot 契约见
[`SKILLS.md`](SKILLS.md)。

模块职责、执行不变量和后续迁移顺序见 [`ARCHITECTURE.md`](ARCHITECTURE.md)，旧实现的
迁入门槛见 [`MIGRATION.md`](MIGRATION.md)。

当前 Provider 契约、OpenAI-compatible Adapter、错误分类和验收命令见
[`PROVIDERS.md`](PROVIDERS.md)。

需要按能力逐步开发和测试时，使用独立下游项目
[`starter/`](starter/README.md)。Starter 当前验收 Step 03：Skill Package；后续仍按单个
能力逐步加入 Flow 等能力，并始终禁止导入 Harness 内部模块。

## 当前边界

首个纵切只实现：

- 不可变 AgentDefinition；
- 类型化 Tool、Toolkit 和 readiness preflight；
- 由受控目录自动发现、受信任 `tools.py` 加载、SkillDescriptor、SkillCatalog 和
  `skill_view` 渐进加载的
  Skill Package，可携带
  Tool/Toolkit、scripts/references/templates/assets 清单和包指纹；
- 固定的 HarnessSnapshot；
- 以 Model Turn 和单轮 ToolCall 上限约束的 model/tool loop；
- Tool 参数、权限、超时和确认治理；
- Provider-neutral text/ToolCall streaming 与完整响应聚合；
- 有序只读 Event；
- 结构化最终输出验证；
- 简单 SequentialFlow 与显式阶段交接。

首批实现明确不包含 HTTP Server、Worker、Lease、持久化恢复、Operator API、插件市场、
动态 Tool Search、复杂多 Agent 调度或产品级会话路由。
