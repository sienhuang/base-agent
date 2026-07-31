# Built-in ToolKit

`base-agent` includes dependency-free Tool factories for the common capabilities a new Agent
needs. They use the same typed schema, permission, timeout, event, and supervision path as
application-defined Tools.

```python
from pathlib import Path

from base_agent import Agent, AgentProfile, basic_tools

tools = basic_tools(workspace_root=Path.cwd())
profile = AgentProfile(
    id="my-agent",
    instructions="Use Tools only when needed.",
    tools=(
        "ask_user",
        "calculate",
        "current_datetime",
        "workspace_list",
        "workspace_read_text",
        "workspace_search_text",
        "list_attachments",
        "read_attachment_text",
        "list_artifacts",
        "read_artifact_text",
        "create_text_artifact",
    ),
    permissions=frozenset(
        {
            "interaction:ask",
            "workspace:read",
            "artifact:read",
            "artifact:write",
        }
    ),
)
```

Passing `workspace_root` is explicit. Workspace Tools resolve every path against that root, reject
path traversal and escaping symlinks, and bound reads, writes, listings, and search matches.
`workspace_write_text` is registered by the factory but should only be exposed by profiles that
also grant `workspace:write`.

## Tool groups

| Factory | Tools | Permissions |
| --- | --- | --- |
| `interaction_tools()` | `ask_user` | `interaction:ask` |
| `utility_tools()` | `current_datetime`, `calculate` | none |
| `workspace_tools(root)` | list, read, write, literal search | `workspace:read`, `workspace:write` |
| `artifact_tools()` | list/read attachments and Artifacts, create text Artifact | `artifact:read`, `artifact:write` |
| `memory_tools()` | `search_memory` | `memory:read` |

`basic_tools()` composes these groups. Memory search requires a configured `MemoryRetriever`.
Workspace Tools are omitted unless a root is supplied.

Sandbox, Browser, and MCP remain optional infrastructure ToolKits:

```python
from base_agent.browser import browser_tools
from base_agent.sandbox import sandbox_tools
```

They are not enabled by `basic_tools()` because they need execution-scoped resources and broader
permissions. Business-specific mutations such as lineage repair remain application Tools.

## Concrete data and development bundles

The following first-party bundles intentionally remain opt-in:

| Bundle | Composition | Permission |
| --- | --- | --- |
| `CodingBundle` | Sandbox read/write/argv Tools plus one Sandbox Resource | `sandbox:*` selected by action |
| `WebSearchBundle` | Bounded `web_search` over a configured Provider | `web:search` |
| `DataSourceBundle` | Catalog/schema Tools plus bounded MTBI / OneSQL queries | `data:read` |

They expose `tools`, `tool_names`, and `required_permissions`; Coding additionally exposes
`resources`. Applications still make the authorization decision by selecting Tool names and
granting permissions in `AgentProfile`.

These are concrete compositions, not a new generic Capability layer. Their common shape will be
evaluated after real adapters and application usage establish stable lifecycle and permission
requirements.
