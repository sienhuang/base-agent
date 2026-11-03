# 初始版本边界

## 目标

建立一个可独立安装、独立运行、可直接定义业务能力的 Python Agent 框架，并把单个
Agent 的模型执行循环交给 DSH。

业务服务使用 `DshApplication` 作为唯一公开装配入口，不需要创建或导入底层 DSH
Runtime factory。

## 框架拥有的能力

- AgentDefinition、Agent snapshot 和预算。
- 一等 Context 定义、Context Catalog、候选、策略、决策提交和输入组合。
- Memory 的作用域、召回、引用解析和写回。
- Tool 的类型 Schema、权限、readiness、超时、副作用、确认和 MCP Gateway。
- Skill 的 playbook、依赖、权限、贡献 Tool 和 Package。
- Sequential 与 Planner/ReAct Flow。
- RunResult、Event、后台 Handle、JSONL EventStore、FastAPI 与 SSE。
- Provider profile、默认模型解析、凭证注入和 Runtime 生命周期。

## 业务注册顺序

```text
DshApplication
  -> register_context(...)
  -> register_tool(...)
  -> register_skill(...)
  -> register_agent(...)
  -> register_flow(...)
  -> fastapi()
```

业务代码不需要接触 `_dsh` 私有包。

## DSH 拥有的能力

- 模型请求和内部执行循环。
- Session 生命周期和 Runtime 进程。
- MCP Tool 调度。
- 原生上下文压缩插件。

框架不会在 DSH 外再运行第二套模型循环。Flow 只编排独立的 Agent Run。

## 一次 Run

```text
HTTP/SDK input
  -> Agent 创建 Run 与 Session
  -> MemoryRuntime 提供候选
  -> ContextRuntime 提交不可变选择
  -> Skill 编译进 persona
  -> ToolGateway 暴露选定 Tool
  -> DSH Runtime 执行模型循环
  -> DSH/Tool 事件写入统一 EventStore
  -> 校验结构化输出和预算
  -> 可选 Memory 写回
  -> 返回 RunResult
```

## 明确限制

- 当前 DSH pin 不提供 Host 级暂停后继续与 Tool confirmation resume。
- 取消通过关闭并逐出对应 Runtime 完成。
- 一个 Agent fingerprint 的 Runtime 串行执行 Run。
- JSONL EventStore 和 HTTP Run 注册表是单进程实现。
