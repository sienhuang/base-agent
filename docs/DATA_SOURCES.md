# MTBI / OneSQL Data Source

`DataSourceBundle` exposes a small provider-neutral data-analysis surface:

- `data_list_tables`;
- `data_describe_table`;
- `data_query`.

All three require `data:read`. There are deliberately no write, DDL, transaction, or arbitrary
connection-management Tools.

```python
from base_agent import (
    Agent,
    AgentProfile,
    MtbiCliDataSource,
    data_source_bundle,
)

data = data_source_bundle(
    MtbiCliDataSource(
        executable="mtbi-cli",
        engine="PRESTO",
        region="volc_cn",
    ),
    max_rows=1_000,
    max_inline_bytes=64_000,
    timeout_seconds=305,
)

agent = Agent(
    profile=AgentProfile(
        id="data-analyst",
        instructions="Inspect schemas before querying and state data limitations.",
        tools=data.tool_names,
        permissions=frozenset({"data:read"}),
    ),
    model=model,
    tools=data.tools,
)
```

## Result boundary

Providers receive an explicit row limit. The Tool applies that limit again and serializes only the
bounded result. If the result exceeds `max_inline_bytes`, it writes the complete bounded JSON
payload as a Run-owned Artifact and returns only:

- columns;
- a bounded sample;
- returned/total row counts when known;
- `has_more`;
- Artifact ID, name, and size.

The Artifact payload also has a configurable maximum. The returned sample and column list are built
under the same byte budget as an inline result, so a single unusually large value cannot inflate
the reference envelope. The SQL text is not copied into Artifact metadata.

## MTBI CLI adapter

`MtbiCliDataSource` maps the portable operations to the company data platform:

- `data_list_tables` → `mtbi-cli meta search --format json`;
- `data_describe_table` → `mtbi-cli meta info --format json`;
- `data_query` → `mtbi-cli onesql --file - --format json`.

The adapter invokes argv directly without a shell and sends SQL through stdin, so SQL is not copied
into the process command line. It accepts one `SELECT` or `WITH` statement, rejects comments and
known mutation/administration keywords, wraps the query in `LIMIT n + 1`, and bounds subprocess
time and output size.

This client-side check is defense in depth, not the authorization boundary. The configured MTBI /
OneSQL identity must itself have read-only access. Authentication remains owned by `mtbi-cli`; do
not put MOA tokens in prompts, Skills, Agent configuration, or Run metadata.

The current adapter waits for a foreground OneSQL result. Detach/fetch/cancel integration and
scan/cost policy remain follow-up work for long-running production queries.

## Starter

After authenticating `mtbi-cli` outside the Agent, enable the capability explicitly:

```bash
uv run agent-app --provider codex-cli --no-skill \
  --mtbi --mtbi-engine PRESTO \
  "Find the relevant table, inspect its schema, and calculate the requested metric"
```

The adapter is opt-in; the default Agent does not receive company data access.
