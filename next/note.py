
--测试tool调用上限
 uv run --project next/starter agent-app \
    --provider codex-cli \
    --max-model-turns 2 \
    --max-tool-calls-per-turn 2 \
    --show-tool-results \
    "In the same model turn, call record_value exactly 3 times with values 1, 2, and 3. Do not answer directly."

--测试tool的权限
uv run --project next/starter agent-app \
    --max-model-turns 2 \
    --grant-permission values:record \
    --show-tool-results \
    --max-tool-calls-per-turn 2 \
    "Call record_value once. calls=1"

--测试tool的参数验证
  uv run --project next/starter agent-app \
    --provider codex-cli \
    --grant-permission values:record \
    --max-model-turns 2 \
    --show-tool-results \
    'Call record_value once. calls=1 tool_args={"value":"abc"}'


--测试tool的参数验证
  uv run --project next/starter agent-app \
    --provider codex-cli \
    --grant-permission values:record \
    --max-model-turns 2 \
    --show-tool-results \
    'Call record_value once. calls=1 tool_args={"value":"abc"}'

--测试tool执行异常测试
  uv run --project next/starter agent-app \
    --provider codex-cli \
    --grant-permission values:record \
    --max-model-turns 2 \
    --show-tool-results \
    'tool=fail_operation calls=1 tool_args={"message":"expected failure"}'

--测试tool执行超时
  uv run --project next/starter agent-app \
    --provider codex-cli \
    --grant-permission values:record \
    --max-model-turns 2 \
    --show-tool-results \
    'tool=slow_operation calls=1 tool_args={"delay_seconds":"0.1"}'

--测试tool-confirmation
 uv run --project next/starter agent-app \
    --grant-permission values:write \
    --approve-side-effects \
    --max-model-turns 2 \
    --show-tool-results \
    'tool=save_value calls=1 tool_args={"value":7}'

--测试tool-readiness ， 默认browser不启用
  uv run --project next/starter agent-app \
    --enable-browser-tool \
    "test browser readiness"

--测试streaming的逻辑
     export OPENAI_API_KEY="" （这个报错了使用qianwen模型报错了， 可是是用ds没有报错的。call_27ec2e2294b64ff3bdfe4309: status=success, data=1
error: final model turn requested Tools while Tool use was disabled）
  uv run --project next/starter agent-app \
    --provider openai-compatible \
    --model deepseek-v4-pro \
    --base-url https://llm.moontontech.net \
    --stream \
    --grant-permission values:record \
    --max-model-turns 2 \
    --show-tool-results \
    'tool=record_value calls=1'




 --测试skill -- 还需验证
  uv run --project next/starter agent-app \
    --provider openai-compatible \
    --model qwen3.7-max \
    --base-url https://llm.moontontech.net \
    --enable-recording-skill \
    --grant-permission values:record \
    --max-model-turns 2 \
    --stream \
    --show-tool-results \
    '记录整数-1'
 --测试skill -- 还需验证
    uv run --project next/starter agent-app \
    --provider openai-compatible \
    --model qwen3.7-max \
    --base-url https://llm.moontontech.net \
    --enable-python-script-skill \
    --grant-permission skills:scripts:execute \
    --show-tool-results \
    --max-model-turns 2 \
    "请严格按照 Python Calculator Skill 完成任务。必须调用 run_python_calculator_script，并使用 scripts/calculate.py 计算 1+2，不能直接心算回答。"

  uv run --project next/starter agent-app \
    --provider openai-compatible \
    --model qwen3.7-max \
    --base-url https://llm.moontontech.net \
    --enable-recording-skill \
    --grant-permission values:record \
    --max-model-turns 3 \
    --show-execution-trace \
    --show-tool-results \
    'tool=skill_view calls=1 tool_args={"name":"value-recording"}'

 --测试skill
  uv run --project next/starter agent-app \ 
    --provider openai-compatible \
    --model qwen3.7-max \
    --base-url https://llm.moontontech.net \
    --enable-python-script-skill \
    --max-model-turns 2 \
    --show-execution-trace \
    --show-tool-results \
    '请使用 python-calculator Skill。必须先调用 skill_view 读取它的完整操作说明；读取后只总结这个 Skill 要求的操作步骤，不要调用
    calculate_sum 或 calculate_product。'

  
  下一步测试权限拒绝。去掉：
  --grant-permission skills:scripts:execute
  执行：
  uv run --project next/starter agent-app \
    --provider openai-compatible \
    --model qwen3.7-max \
    --base-url https://llm.moontontech.net \
    --enable-python-script-skill \
    --max-model-turns 3 \
    --show-execution-trace \
    --show-tool-results \
    '请使用 python-calculator Skill 计算 4×5。必须先调用 skill_view，然后
    按照 Skill 调用对应 Tool。如果 Tool 因权限不足被拒绝，不要自行计算，
    必须如实说明。'

---flow 测试和验证。
   uv run --project next/starter agent-app \
    --provider openai-compatible \
    --model qwen3.7-max \
    --base-url https://llm.moontontech.net \
    --flow plan-react \
    --show-execution-trace \
    --show-tool-results \
    "计算 4 * 30"

  确定性离线测试 Tool 调用：

  uv run --project next/starter agent-app \
    --provider openai-compatible \
    --model deepseek-v4-pro \
    --base-url https://llm.moontontech.net \
    --flow plan-react \
    --show-execution-trace \
    --show-tool-results \
    'tool=calculate_product calls=1 tool_args={"left":4,"right":30}'

  ###TODO

  ### 这一阶段应该测试什么

  1. 启用 Skill 后，Skill instructions 确实进入最终 System Prompt。
  2. Skill 声明的 Tool 自动对该 Agent 可见。
  3. 没有启用 Skill 时，该 Skill 的 Tool 不可见。
  4. Skill 引用了不存在的 Tool 时，在 Agent 构造阶段失败。
  5. Agent 缺少 Skill 所需权限时，在模型请求之前失败。
  6. HarnessSnapshot 记录本次 Run 使用了哪些 Skill。
  7. Skill + ToolCall + Streaming 可以一起正常工作。
  8. Skill 不直接接触 Harness、Provider 或 Runtime Context。

  现有 Skill 类型已经在 next/src/base_agent/skills/models.py:12，SDK 也有基础单元测试，但 Starter 还没有把它作为开发者使用场景完整验收。

  ### 后续合理顺序

  我建议接下来按这个顺序：

  1. Starter 验收 Skill。
  2. Starter 验收两个 Agent 的 SequentialFlow 和显式 handoff。
  3. 增加 RunHandle/start()，把执行和事件消费解耦。
  4. 在 RunHandle 之上增加 HTTP SSE。
  5. 增加取消和小型 ContextPolicy。
  6. 最后再考虑 Planner/Executor。