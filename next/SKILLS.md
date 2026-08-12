# Skill Package contract

Skill 是某类任务的可复用经验和作业手册。核心是 `SKILL.md` 中的适用条件、操作步骤、
判断规则、失败恢复和完成标准；Tool、Script、Reference 和 Asset 是执行这套经验时可以
使用的资源。

Skill 不是简化版 Flow：Skill 的步骤由模型根据现场信息灵活选择、跳过和重试；Flow 的
Stage 顺序由 Runtime 强制执行。

## 1. 标准目录

一个 Skill 使用一个独立目录：

```text
skills/
└── data_analysis/
    ├── SKILL.md
    ├── tools.py
    ├── scripts/
    ├── references/
    ├── templates/
    └── assets/
```

- `SKILL.md` 必需，frontmatter 提供可扫描的身份信息，正文是 Skill playbook；
- 纯指令型 Skill 只需 `SKILL.md`，不需 Python 注册文件；
- `tools.py` 可选；在应用显式信任的 Skill root 中，其本地 `@tool` 函数会自动加载；
- `scripts/` 下的 Python、Shell 等普通文件自动属于这个 Skill，不需要逐个注册；
- `references/`、`templates/`、`scripts/` 和 `assets/` 进入资源清单和包指纹；模型通过
  `skill_view` 按需读取，不会在 Run 开始时被整体塞入上下文。

目录中的符号链接被拒绝。`__pycache__` 和 `.pyc` 不进入资源清单与指纹。

`SKILL.md` 的标准头部为：

```markdown
---
name: data-analysis
description: Inspect structured data and produce an evidence-backed report.
version: 1.0.0
---

# Data Analysis
...
```

自动扫描的 Skill 必须提供 `name` 和 `description`；`version` 可省略，默认为
`1.0.0`。frontmatter 使用 YAML，数据只作为声明式元数据解析，不执行代码。

## 2. Playbook 是核心

`SKILL.md` 应描述经验，而不是只列出 Tool：

```markdown
# Data Analysis

## When to use
当用户要求检查结构化数据、发现异常或生成统计报告时使用。

## Procedure
1. 先检查输入格式和字段。
2. 数据量较大时运行 profile_data 脚本。
3. 发现异常值后，按 reference 中的规则判断是否需要清洗。
4. 必要时调用共享 query_database Tool 补充数据。
5. 使用 render_report Tool 生成报告。

## Recovery
- 输入损坏时停止分析并指出损坏位置。
- 可选数据库 Tool 不存在时，只使用现有数据并声明限制。

## Completion criteria
报告必须包含数据范围、直接证据、异常项和限制。
```

## 3. 简单代码型 Skill

普通开发者只需在 `tools.py` 中写一个或多个类型化 Tool：

```python
from base_agent import tool

from .scripts.render import render


@tool(permissions=frozenset({"reports:write"}))
async def render_report(title: str, body: str) -> str:
    return render(title, body)
```

如果一个 Skill 包含多个 `@tool`，它们全部会自动加入该 Skill。Script 默认是 Tool
的私有实现；模型只看到 `render_report`，不需要知道 `scripts/render.py`。每个 Tool
独立声明参数、权限、副作用、确认和超时。

默认会收集 `tools.py` 中本地声明的所有 `@tool`。需要只暴露其中一部分，或组合
Toolkit/自定义 Tool 时，可显式导出：

```python
TOOLS = (render_report,)
```

`SkillDependencies`、`SkillPermissions`、`SkillScriptExecution` 仍保留给确实需要外部
Tool 依赖、整体 Skill 前置权限或直接子进程 Script Runner 的高级适配器；它们
不是普通 Skill 的必填内容。

## 4. Descriptor、Catalog 与渐进加载

`SkillDescriptor` 是不包含 playbook 的轻量元数据：名称、描述、版本、来源和 Package
fingerprint。`SkillCatalog` 是不可变的 Skill 目录，负责发现、按名称选择和解析
Skill。

外部或不可信目录使用只读发现：

```python
from pathlib import Path

from base_agent import SkillCatalog

SKILL_CATALOG = SkillCatalog.discover(Path(__file__).parent)
```

应用自己维护的 Skill 目录使用可执行发现：

```python
SKILL_CATALOG = SkillCatalog.discover_trusted(Path(__file__).parent)
```

`discover_trusted()` 是明确的信任决策：它只自动导入每个 Skill 根下的固定
`tools.py`，而 `tools.py` 自己的 import 也会执行。不应对下载、用户上传或未审查
目录调用它。安全 `discover()` 永远不导入 Python。两种模式都会拒绝重名、
嵌套 Package 和符号链接。

高级适配器仍可使用 `definitions=` 按 Package root 覆盖自动构造结果，但 Starter
和普通业务 Skill 不需要维护这个中央列表。

应用把 Catalog 交给 Agent，但不重复注册 Skill 自己贡献的 Tool：

```python
agent = Agent(
    AgentDefinition(
        ...,
        skills=("data-analysis",),
        permissions=frozenset({"files:read"}),
    ),
    model=model,
    tools=(read_file,),
    skills=SKILL_CATALOG,
)
```

`AgentDefinition.skills` 只选择本次 Run 可使用的 Skill。Harness 在 system Prompt 中只加入
被选 Skill 的 Descriptor；模型需要先调用稳定的 `skill_view(name)`，完整 `SKILL.md` 才会
作为 ToolResult Observation 进入下一轮上下文：

```text
SkillCatalog
  → SkillDescriptor 列表进入 system Prompt
  → 模型调用 skill_view(name="data-analysis")
  → 完整 SKILL.md + supporting file 清单
  → 必要时 skill_view(name="data-analysis", file_path="references/rules.md")
```

`skill_view` 只读取经验和 supporting files，不执行脚本，也不能在 Run 中新增 Tool schema。
Skill contributions 和 Script Tool 仍须在 Run 开始前确定并固定进 Snapshot。

一次 `skill_view` 必须完整装入单条 Observation；超过当前 15,000-byte 载荷上限时会失败，
不会悄悄截断并伪装成已加载。大型 Skill 应把细节拆到多个 supporting files，由 playbook
明确指出何时按需读取。

## 5. Script 定位和执行边界

一个 Skill 可以直接包含多个不同语言的 Script：

```text
scripts/
├── profile_data.py
├── render_report.sh
└── helpers/
    └── normalize.py
```

Package Loader 自动把这些普通文件记录为 `script_files` 并固定进 Snapshot；开发者不需要
再写 `SkillScript(...)`。`__init__.py`、隐藏文件、`__pycache__` 和 `.pyc` 不进入 Script
清单，但仍可在适用时进入整个 Package 指纹。

文件属于 Skill 不等于获得执行权限。默认情况下，Script 由 `tools.py` 中的类型化
Tool 作为普通 Python 模块调用；Harness 治理的是这个 Tool 边界。

只有通用自动化 Agent 确实需要让模型选择文件路径时，才使用高级直接 Script
执行适配器：

```python
from base_agent.skills import (
    SkillPermissions,
    SkillScriptExecution,
    SkillScriptRuntime,
)

PYTHON_SKILL = Skill.from_package(
    ...,
    permissions=SkillPermissions(
        required=frozenset({"skills:scripts:execute"}),
    ),
    script_execution=SkillScriptExecution(
        runtimes=frozenset({SkillScriptRuntime.PYTHON}),
        permission="skills:scripts:execute",
        timeout_seconds=5,
        max_output_bytes=16_384,
    ),
)
```

该高级定义会生成 `run_<skill_name>_script` Tool。例如
`automation-scripts` 对应 `run_automation_scripts_script`。调用参数是 Package 内的相对
`script` 路径和 JSON `payload`。Python Script 从 stdin 读取 JSON，并把 JSON 或文本写到
stdout。

直接执行会检查：Skill 已选中、执行权限、Script 位于自动发现清单、`.py` Runtime 已启用、
Package 指纹未变化、固定工作目录、最小环境、超时和 stdout/stderr 大小。SDK 使用当前
Python 解释器直接启动文件，不经过 Shell。

已经接入的执行链仍然是：

```text
模型 ToolCall
  → Skill contribution Tool
  → 参数、权限、副作用、确认、readiness、超时检查
  → internal script
  → ToolResult / Observation
```

直接 Python Script 的执行链是：

```text
模型调用 run_<skill>_script
  → Harness Tool 权限与治理
  → Skill Package 路径和指纹检查
  → Python 子进程（JSON stdin/stdout）
  → ToolResult / Observation
```

## 6. Snapshot 与可复现性

`Skill.from_package()` 会读取 `SKILL.md`、登记包文件，并对包内容生成 SHA-256 指纹。
`HarnessSnapshot` 固定记录：

- Skill 名称与版本；
- Package fingerprint；
- required/optional Tool dependencies；
- contributed Tool 名称；
- required/optional permissions；
- 从 `scripts/` 自动发现的相对文件路径；
- Script Runtime、执行权限、超时和输出上限；
- references/templates/assets 相对路径；
- Descriptor 索引合成后的 system Prompt 和最终 Tool schema。

Run 开始后，本次 Run 使用已经解析的 playbook、能力和 snapshot。`skill_view` 返回已经选定
的 playbook；读取 supporting file 前会重新校验整个 Package fingerprint，检测到磁盘变化
时拒绝混用新旧内容并要求重建 Agent。

## 7. Skill 使用追踪

Harness 区分“已加载”和“资源确实进入执行”两个客观事实：

```text
run.started
  → skill.selected
  → model.requested / model.responded
  → tool.started(skill_view)
  → tool.completed(skill_view)
  → skill.loaded
  → model.requested / model.responded
  → skill.resource.used
  → tool.started
  → tool.completed
  → model.requested / model.responded
  → run.succeeded
```

- `skill.selected` 表示 Skill 已被 AgentDefinition 选中并固定进本次 Run snapshot；
- `skill.loaded` 只在模型成功调用 `skill_view` 读取完整 `SKILL.md` 时产生；
- `skill.file.loaded` 表示模型通过 `skill_view` 读取了一个 supporting file；
- `skill.resource.used` 只在 Skill 自带 Tool 或 Runtime 生成的 Script Tool 通过 Tool 查找、
  权限、参数和确认检查，即将执行时产生；
- Script 事件记录 Skill 名称、版本、Package 指纹、相对 Script 路径、Tool 名称和
  `tool_call_id`；
- `tool.completed` 的 status 才表示资源最终成功、超时或执行错误；
- shared dependency Tool 不归 Skill 所有，因此不会被强行归因给某一个 Skill；
- `skill.loaded` 可以证明 playbook 确实进入了上下文，但仍不能证明模型严格遵循了每一步；
  `skill.resource.used` 只记录可客观归因的 Tool 或 Script 使用。

这些 Event 先写入 Agent 配置的 EventStore，再实时通知构造期 Observer 和单次 Run 的
`on_event`，最终由同一 Store 生成 `RunResult.events`。Observer 是 fail-open 的实时消费
通道，不承担可靠持久化；完整边界见 [`EVENTS.md`](EVENTS.md)。

## 8. 当前边界

- 普通 Skill 使用受信任 `tools.py` 包装 Script，不向模型暴露文件路径；
- 直接 Script Runner 默认关闭，只有高级 Skill 定义显式 opt-in 才启用；
- 当前直接执行只支持 Python，Shell 等文件仅自动发现和审计；
- supporting files 只允许从 Package 清单按相对路径读取，并拒绝目录穿越；
- 不提供任意生命周期 Hook，避免绕过 Harness；
- Catalog 只扫描应用显式传入的 root，不会默认遍历用户主目录或环境；
- `discover_trusted()` 只能用于应用拥有并审查的目录；外部 Skill 必须使用不导入
  Python 的 `discover()`；
- `skill_view` 不动态改变 Tool schema；
- 不允许 Skill 自己创建 Worker、Store 或独立 Runtime。

这些能力需要形成独立、可测试的契约后再增加。
