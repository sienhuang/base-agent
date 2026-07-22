# Writing Skills

A Skill is a versioned procedure with explicit Tool and permission boundaries. It is not an Agent
subclass and it is not an unrestricted prompt fragment.

## Package layout

```text
skills/
└── order-refund/
    ├── SKILL.md
    ├── references/
    └── schemas/
```

Only `SKILL.md` is required by the first runtime version.

## Manifest

```yaml
---
name: order-refund
version: 1.0.0
description: Validate and prepare an order refund.
argument-hint: "[order_id]"
allowed-tools:
  - get_order
  - prepare_refund
required-tools:
  - get_order
required-permissions:
  - orders:read
---

1. Load the order.
2. Verify that it is refundable.
3. Prepare a proposal; do not execute a payment automatically.
```

`required-tools` must be a subset of `allowed-tools`. An empty allowlist means the Skill cannot call
Tools. The Runtime records the exact selected Skill version in the Run and event history.

## Register and select

```python
registry = SkillRegistry.from_directory(Path("./skills"))

profile = AgentProfile(
    id="order-agent",
    instructions="Follow selected Skills.",
    tools=("get_order", "prepare_refund"),
    skills=("order-refund",),
    permissions=frozenset({"orders:read"}),
)

agent = Agent(
    profile=profile,
    model=model,
    tools=[get_order, prepare_refund],
    skill_registry=registry,
)

result = await agent.run("Refund order 10001", skills=("order-refund",))
```

Skill selection is explicit in the current API. Semantic selection is intentionally deferred.

## Test a Skill alone

```python
harness = SkillHarness(registry, [get_order, prepare_refund])
report = harness.validate("order-refund", profile=profile)
assert report.valid, report.issues
```

Registration reads only Manifest front matter. Full instructions are loaded after explicit
selection or Harness validation.
