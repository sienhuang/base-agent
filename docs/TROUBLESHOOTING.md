# Troubleshooting

## `tool 'x' is not registered`

The profile names a Tool that was not passed to `Agent(tools=[...])`. Register the Tool and keep the
same public name in `AgentProfile.tools`.

## `tool_not_allowed`

The Tool exists but is outside the active Profile or selected Skill allowlist. Add it only if the
capability is intentionally required; do not bypass the executor check.

## `permission_denied`

The Tool or Skill requires a permission absent from `AgentProfile.permissions`.

## `FakeModel has no scripted responses remaining`

The Runtime made more model steps than the test scripted. Inspect `FakeModel.requests` and add the
missing response, or fix an unexpected loop.

## `maximum model steps` or `maximum tool calls`

The default `ExecutionBudget` stopped the Run. Inspect `supervisor.intervened` events before raising
limits. Repeated calls usually indicate a Tool result or Skill instruction problem.

## `VIRTUAL_ENV ... does not match`

When running `uv` from another activated project, `uv` ignores that environment and uses this
project's `.venv`. Deactivate the outer environment to remove the warning; it does not change which
environment `uv` selects.
