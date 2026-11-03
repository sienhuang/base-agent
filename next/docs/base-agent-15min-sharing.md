---
marp: true
theme: default
paginate: true
size: 16:9
title: 从模型调用到可靠运行：base-agent 的工程化实践
description: 面向团队技术同学的 15 分钟技术分享
---

<!--
分享时长：约 15 分钟，另留 3～5 分钟问答。
演示前请按 docs/base-agent-sharing-demo.md 完成预演。
-->

# 从模型调用到可靠运行

## base-agent 的工程化实践

> 模型负责决策，Runtime 负责控制、验证和恢复

<!--
讲稿（约 30 秒）：

今天不重点讨论怎么写 Prompt，也不讨论哪个模型更强。我们讨论的是，当 Agent 真正进入业务系统以后，如何控制 Tool、记录执行过程、等待用户输入，以及在失败后安全恢复。base-agent 主要解决的就是这些模型之外的问题。

转场：先从最常见的 Agent 实现开始。
-->

---

# 一个 Agent Demo 离生产还有多远？

```python
while True:
    response = model(messages, tools)
    if response.tool_calls:
        execute_tools(response.tool_calls)
    else:
        return response
```

- 模型可以调用哪些 Tool？
- 参数、权限和高风险操作由谁检查？
- 缺少用户输入时怎么办？
- 进程中断后从哪里继续？
- 执行过程和产物如何审计？

<!--
讲稿（约 2 分钟）：

写出一个能调用 Tool 的循环并不困难，几十行代码就可以完成。但这个循环还没有回答五个生产问题。

第一，模型能看到和执行哪些能力；第二，谁在执行前校验参数、权限和副作用；第三，信息不足时如何暂停，而不是让模型猜测；第四，进程中断后能不能安全继续；第五，我们能不能回答某个结果是谁、在哪一次运行、通过哪个 ToolCall 产生的。

如果这些问题没有答案，它更接近一个 Demo，而不是可以进入业务系统的 Runtime。

转场：base-agent 的设计就是围绕这些边界展开的。
-->

---

# base-agent 的核心主路径

```text
Prompt + Tool + Skill
        │
        ▼
AgentDefinition + Agent
        │
        ▼
      Harness
  model/tool loop
  governance/events
  waiting/verification
        │
        ▼
     RunResult
```

**Agent 是能力定义，Harness 是执行语义。**

<!--
讲稿（约 2 分钟）：

base-agent 故意把开发者主路径压得很窄。

AgentDefinition 声明这个 Agent 使用什么 Prompt、模型、Tool、Skill、权限和执行预算。Agent 是应用侧的组合门面，负责选择并检查这些依赖。Harness 是包内的运行内核，负责一次有限 Run 的真实执行。最终所有路径收敛为 RunResult。

一个重要边界是：普通应用不需要直接操作 Harness。如果 starter 为了完成业务场景必须导入 runtime 内部模块，就说明公共 API 的设计出现了问题。

转场：下面看 Harness 为一次 Run 增加了哪些确定性。
-->

---

# 一次 Run 是怎样执行的？

```text
冻结 Prompt / Tool / Skill / 权限 / 模型
                    ↓
             Tool readiness
                    ↓
                请求模型
                    ↓
              模型返回 ToolCall
                    ↓
       参数校验 → 权限检查 → 人工确认
                    ↓
        执行 Tool → Event → Tool Result
                    ↓
          再次请求模型 / 校验最终结果
```

```text
CREATED → RUNNING ⇄ WAITING
               ├→ SUCCEEDED
               ├→ FAILED
               └→ CANCELLED
```

<!--
讲稿（约 3 分钟）：

一个 Run 开始后，它的 system Prompt、Tool Schema、Skill、权限和模型选择都会被冻结。模型可以在允许的范围内动态选择 Tool，但不能在运行中扩大自己的能力。

第一次模型请求前会完成 readiness。模型产生 ToolCall 后，Host 先校验参数，再检查权限和人工确认，最后才真正执行 Tool。Tool 结果受到单次和整次 Run 的 observation budget 限制，避免一个过大的结果拖垮后续上下文。

如果 Tool 表示还需要用户输入，Run 会进入 WAITING。此时 Harness 写入 Checkpoint 并返回，不占住线程。用户回复后，框架把回复作为原 tool_call_id 的 Tool result，继续原来的 Run，而不是从头重跑。

最终的 succeeded、failed 和 cancelled 都是明确且互斥的终态。

转场：这些状态为什么不直接全部保存到一份聊天记录里？
-->

---

# 可靠性来自明确的状态边界

| 概念 | 回答的问题 | 典型用途 |
|---|---|---|
| Event | 发生了什么？ | 观察、回放、审计 |
| Checkpoint | 从哪里继续？ | WAITING、恢复 |
| Artifact | 正式提交了什么？ | 不可变业务产物 |
| Resource | 执行期间使用什么？ | workspace、连接、客户端 |

> 多保存日志不等于可恢复；临时文件也不等于正式产物。

<!--
讲稿（约 2 分钟）：

这四个概念都与状态有关，但不能互相替代。

Event 是已经发生的事实，用于观察和审计；Checkpoint 保存经过验证的继续位置，所以知道 Event 不代表一定知道怎样安全恢复。Artifact 是 Run 正式提交的不可变内容；Resource 是只在 Run 期间存活的能力，例如 workspace 或数据库连接。

这种拆分看起来增加了类型和存储，但换来的是更明确的恢复、授权和审计语义。

转场：同样的原则也体现在多 Agent 编排上。
-->

---

# 多 Agent：模型建议，Host 决策

```text
              Planner-ReAct Flow

PLANNING → EXECUTING(one Step) → REVIEWING
                 ↑                   │
                 └──── revise ───────┘
                              └─ complete / fail
```

- Planner：创建、Review 结构化 Plan
- Executor：一次只完成一个 Host 选中的 Step
- Host：掌握 ID、状态转换、预算和恢复
- 每次 Planner / Executor 调用都是独立 Agent Run

<!--
讲稿（约 2 分钟）：

base-agent 没有把多 Agent 设计成几个模型自由聊天。Planner 只能返回结构化 PlannerDecision，不能直接修改 PlanStore，也不能生成系统 ID。Executor 一次只执行 Host 选中的一个 Step。

执行完成后，Planner 可以保留或修改尚未执行的后缀，但不能重写已经执行的前缀。Plan、Step 和子 Run 的关联都是 Host 提交的事实。

如果流程是固定阶段，使用 SequentialFlow；只有确实需要动态规划和逐步 Review 时，才使用 Planner-ReAct。

核心原则是：Planner 可以建议下一步，但不能成为系统状态的权威来源。

转场：最后用一个确定性演示把这些概念串起来。
-->

---

# Demo：Plan → Tool → Review

```bash
uv run --project starter agent-app \
  --provider offline \
  --flow plan-react \
  --show-execution-trace \
  --show-tool-results \
  'tool=calculate_product calls=1 tool_args={"left":4,"right":30}'
```

```text
planner.started
      ↓
step.started
      ↓
tool.completed → product=120
      ↓
planner.completed(action=complete)
```

<!--
讲稿（约 3 分钟）：

这里故意使用 OfflineProvider，让演示可以确定性复现。重点不是展示模型会算乘法，而是观察一个任务如何经过 Plan、独立 Executor Run、ToolCall 和 Review。

运行命令后只指出四组事件：
1. planner.started 创建计划；
2. step.started 启动一个独立的 Executor Run；
3. tool.completed 返回 product=120，随后 Executor 完成；
4. Planner 再次以 review 身份启动，并返回 action=complete，最终 Flow succeeded。

提醒听众观察 Flow 和子 Run 使用不同 ID 与 sequence。它们可以关联，但不会混成一份模糊的聊天历史。

如果现场时间不足，本页只展示预先保存的终端输出，不实际执行命令。

转场：最后总结 base-agent 真正提供的价值。
-->

---

# 三个结论

1. **Agent 的智能来自模型，可靠性来自 Runtime**

2. **模型负责提出行动，Host 负责验证、执行和提交事实**

3. **多 Agent 的价值不在数量，而在清晰的控制、治理和恢复边界**

> 代价：更多类型、状态和持久化复杂度；换来可测试、可观察和可恢复。

<!--
讲稿（约 1 分钟）：

最后希望大家记住三句话。

第一，模型能力决定 Agent 的智能上限，但 Runtime 决定它能不能可靠地进入业务系统。第二，模型输出是建议，只有经过 Host 校验和提交后才成为系统事实。第三，多 Agent 不是 Agent 越多越好，只有出现真正的治理、上下文、验证或恢复边界时才值得拆分。

base-agent 最重要的价值，不是让模型变得更聪明，而是让模型的不确定行为进入一个确定、可检查、可恢复的工程系统。

随后进入问答。
-->
