# DSH 集成边界复核

## 调用方向

```text
dsh_base_agent.Agent
  -> DshRuntime Protocol
  -> SdkRuntimeFactory
  -> official deepseek_harness.DeepSeekHarness
  -> DSH child process
```

业务层只依赖窄的 `DshRuntime` Protocol，因此单元测试可以使用 `ScriptedRuntimeFactory`，
真实 SDK 的同步进程调用由 adapter 放入 worker thread。

## Profile 所有权

每个不可变 Agent fingerprint 生成独立的 DSH home 和 profile patch。Patch 负责：

- 写入 Agent persona。
- 设置 DSH compaction 参数。
- 默认关闭 DSH 原生通用 Tool。
- 只在选中业务 Tool 时安装本地 MCP client。

`allow_native_tools=True` 是显式逃生口，不是默认行为。

## 生命周期

- `BaseAgentApp` 为每个 fingerprint 持有一个 Runtime。
- 同 fingerprint 的 Run 使用锁串行进入 Runtime。
- ToolGateway 在 Runtime 启动前 ready。
- Runtime 先关闭，MCP Gateway 后关闭。
- DSH 调用异常或取消后逐出 Runtime，避免复用状态不确定的子进程。

## 风险

- 原始 DSH 事件可能包含 reasoning、请求头、persona 或 Tool Schema；对外服务需要事件
  分类、过滤与脱敏。
- Runtime pin 为预发布版本，升级必须经过 profile、协议和真实模型回归。
- 当前本地状态实现不支持多 worker 协作。
