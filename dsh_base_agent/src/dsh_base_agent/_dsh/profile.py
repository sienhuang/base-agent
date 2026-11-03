"""Generated invocation patch for the pinned full DSH ``sdk`` Profile."""

from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import uuid4

from dsh_base_agent._dsh.protocol import RuntimeSpec


class DshProfileError(RuntimeError):
    pass


_NATIVE_TOOL_ROWS = (
    "tool-bash",
    "tool-pwsh",
    "tool-jobs",
    "tool-fs",
    "tool-fs-search",
    "tool-skill",
    "plan-mode",
    "tool-subagent-control",
    "tool-subagent-list-agents",
    "tool-subagent",
    "tool-subagent-fork",
    "tool-subagent-report",
    "tool-workflow",
    "tool-todo",
    "tool-goal",
    "tool-ralph",
    "tool-str-replace-editor",
    "tool-web",
)


class GeneratedSdkProfilePatches:
    """Materialize the Agent-owned portion of the full ``sdk`` Profile.

    JSON is emitted because it is a strict subset of the DSH YAML patch grammar.
    The full Profile itself remains owned by the same-version bundled Runtime.
    """

    def __call__(self, spec: RuntimeSpec, dsh_home: Path) -> tuple[Path, ...]:
        if spec.tool_names and spec.tool_gateway_url is None:
            raise DshProfileError("selected Python Tools require a loopback MCP gateway")
        context = spec.context_configuration or {}
        mode = context.get("mode")
        patches: list[dict[str, object]] = [
            {
                "id": "system-prompt",
                "config": {"persona": spec.instructions},
            }
        ]
        if not spec.allow_native_tools:
            patches.extend({"id": row, "disabled": True} for row in _NATIVE_TOOL_ROWS)
        if spec.tool_gateway_url is not None:
            patches.append(
                {
                    "insert": [
                        {
                            "id": "base-agent-mcp-tools",
                            "name": "@deepseek-ai/dsh-mcp-client",
                            "config": {
                                "serverName": "dsh_base_agent",
                                "transport": "streamable-http",
                                "url": spec.tool_gateway_url,
                            },
                        }
                    ]
                }
            )
        if mode == "keep-all":
            patches.append({"id": "compaction-basic", "disabled": True})
        elif mode == "compaction-basic":
            patches.append(
                {
                    "id": "compaction-basic",
                    "config": {
                        "thresholdRatio": context["thresholdRatio"],
                        "retainRatio": context["retainRatio"],
                    },
                }
            )
        else:
            # Custom ContextPolicy output is admitted as source-tagged content
            # in the Run's first DSH user message. Session compaction remains a
            # separate native concern and is disabled unless explicitly chosen.
            patches.append({"id": "compaction-basic", "disabled": True})

        patch_dir = dsh_home / "base-agent-patches"
        patch_dir.mkdir(parents=True, exist_ok=True)
        path = patch_dir / f"{spec.fingerprint}.patch.json"
        serialized = json.dumps(
            patches,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if path.exists() and path.read_text(encoding="utf-8") == serialized:
            return (path,)
        temporary = path.with_suffix(f".{uuid4().hex}.tmp")
        try:
            with temporary.open("x", encoding="utf-8") as handle:
                handle.write(serialized)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            directory_fd = os.open(patch_dir, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if temporary.exists():
                temporary.unlink()
        return (path,)
