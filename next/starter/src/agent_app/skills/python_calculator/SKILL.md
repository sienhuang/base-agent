---
name: python-calculator
description: Calculate a sum by executing a packaged Python script.
version: 1.0.0
---

# Python Calculator

## When to use

Use this Skill when the user asks to add or multiply two numbers through packaged Python logic.

## Procedure

1. Call `calculate_sum` for addition or `calculate_product` for multiplication with numeric
   `left` and `right` fields.
2. Read the ToolResult and answer with the returned sum.

## Completion criteria

Do not calculate the result yourself when this Skill is explicitly requested. Report a Tool error instead of inventing a result.
