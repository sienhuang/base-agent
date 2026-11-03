# base-agent 15 分钟分享：演示运行手册

这份手册配合 `docs/base-agent-15min-sharing.md` 使用。演示本身控制在 3 分钟以内。

## 1. 演示目标

演示重点不是模型计算能力，而是以下执行链路：

```text
Planner 创建 Plan
        ↓
Executor 启动独立 Run
        ↓
Executor 调用 Tool
        ↓
Planner Review
        ↓
Flow 提交成功终态
```

## 2. 分享前预检

在仓库 `next/` 目录执行：

```bash
uv run --project starter agent-app \
  --provider offline \
  --flow plan-react \
  --show-execution-trace \
  --show-tool-results \
  'tool=calculate_product calls=1 tool_args={"left":4,"right":30}'
```

成功标准：

- 命令退出码为 `0`；
- 出现 `plan.created`；
- `calculate_product` 的结果为 `product=120`；
- 出现 `plan.completed` 和 `flow.succeeded`。

## 3. 现场讲解顺序

不要逐行解释全部 Event，只讲以下五个观察点：

1. `planner.started phase=create`：Planner 是一个没有 Tool 的独立 Agent Run。
2. `plan.created`：结构化 PlannerDecision 由 Host 校验并提交为 Plan。
3. `step.started`：Executor 使用新的 `agent_run_id` 执行一个 Step。
4. `tool.completed ... status=success`：Tool 返回 `product=120`，随后 Executor 在第二个 Model Turn 完成结果。
5. `planner.started phase=review`：Planner Review 已执行结果并选择 `complete`，Flow 最终成功。

## 4. 推荐现场台词

> 这里使用 OfflineProvider，是为了让执行结果可以确定复现。我们现在不关注模型会不会算乘法，只关注一次任务如何被 Runtime 拆成可以验证和追踪的事实。

> 可以看到 Planner、Executor 和 ToolCall 都有独立 ID。Flow Event 负责记录编排事实，子 Run Event 负责记录模型和 Tool 的执行细节，两者通过 ID 明确关联。

> 最后并不是模型直接宣布整个 Flow 成功，而是 Planner 给出结构化 Review，Host 校验后提交 `plan.completed` 和 `flow.succeeded`。

## 5. 备用方案

分享前把一次成功输出保存为终端截图。如果现场环境、字体或投屏出现问题，直接展示截图并按以下顺序圈出：

```text
planner.started
plan.created
step.started
tool.completed
step.completed
planner.started phase=review
plan.completed
flow.succeeded
```

截图中可以隐藏完整 UUID，但至少保留前八位，让听众看到 Flow 与子 Run 使用不同 ID。

## 6. 常见问答

### 为什么 Event 不能直接用于恢复？

Event 描述已经发生的事实；Checkpoint 保存经过验证的继续位置、固定运行快照和待完成 ToolCall。知道历史并不自动意味着可以安全重放副作用。

### 为什么 Planner 不能直接修改 Plan？

模型输出不是权威状态。Planner 只能提交结构化建议，Host 负责校验 Plan revision、已执行前缀、预算和系统 ID。

### Skill 和 Tool 的区别是什么？

Tool 是类型化的可执行能力；Skill 是包含操作经验、依赖、权限和能力贡献的可发现包。Skill 可以贡献 Tool，但不能绕过 AgentDefinition 的能力和权限边界。

### 能保证外部副作用 exactly-once 吗？

不能天然保证。base-agent 使用确认、稳定操作身份、Attempt fencing、副作用证据和保守恢复来降低重复执行风险；证据不足时进入人工恢复，而不是自动重放。

### 为什么不让一个 Agent 完成所有步骤？

默认应使用单 Agent。只有存在独立的治理、上下文、验证、模型、预算、审计或恢复边界时，才值得拆成多个 Run 或 Flow Stage。
