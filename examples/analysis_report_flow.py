"""A realistic two-Agent Flow with isolated Tool access and explicit handoff."""

import asyncio

from base_agent import (
    Agent,
    AgentDefinition,
    Flow,
    ModelResponse,
    ToolCall,
    ToolSideEffectMode,
    tool,
)
from base_agent.testing import FakeModel


@tool(side_effect=ToolSideEffectMode.READ_ONLY)
async def load_business_metrics(period: str) -> dict[str, object]:
    """Load a bounded business-metrics snapshot for one reporting period."""
    return {
        "period": period,
        "revenue": 1_240_000,
        "revenue_growth_percent": 12.4,
        "active_customers": 8_420,
        "customer_growth_percent": 7.1,
    }


async def main() -> None:
    analyst_model = FakeModel(
        [
            ModelResponse(
                tool_calls=(
                    ToolCall(
                        id="load-q2-metrics",
                        name="load_business_metrics",
                        arguments={"period": "2026-Q2"},
                    ),
                )
            ),
            ModelResponse(
                content=(
                    "2026-Q2 revenue was 1.24M, up 12.4%; active customers "
                    "reached 8,420, up 7.1%. Revenue grew faster than the "
                    "customer base."
                )
            ),
        ]
    )
    writer_model = FakeModel(
        [
            ModelResponse(
                content=(
                    "2026-Q2 showed healthy growth: revenue increased 12.4% "
                    "to 1.24M while active customers rose 7.1% to 8,420. "
                    "The faster revenue growth suggests improved value per "
                    "customer."
                )
            )
        ]
    )

    analyst = Agent(
        definition=AgentDefinition(
            id="business-analyst",
            version="1.0.0",
            instructions=(
                "Load the requested metrics and produce a factual bounded "
                "analysis for the report writer."
            ),
            tools=("load_business_metrics",),
            permissions=frozenset({"metrics:read"}),
        ),
        model=analyst_model,
        tools=(load_business_metrics,),
    )
    writer = Agent(
        definition=AgentDefinition(
            id="report-writer",
            version="1.0.0",
            instructions=(
                "Write an executive summary using only the task and explicit "
                "handoff. Do not invent metrics."
            ),
        ),
        model=writer_model,
    )

    flow = Flow.sequence(
        {"analyst": analyst, "writer": writer},
        id="quarterly-analysis-report",
    )
    run = await flow.run("Analyze 2026-Q2 and write an executive summary.")

    print(run.output)


if __name__ == "__main__":
    asyncio.run(main())
