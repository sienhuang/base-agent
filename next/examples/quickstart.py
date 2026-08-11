"""Offline Tool + Skill + two-Agent Flow example."""

import asyncio

from base_agent import (
    Agent,
    AgentDefinition,
    Flow,
    Skill,
    Stage,
    tool,
)
from base_agent.models import ModelResponse, ToolCall
from base_agent.skills import SkillContributions, SkillPermissions
from base_agent.testing import FakeModel


@tool(permissions=frozenset({"sales:read"}))
async def get_sales(period: str) -> dict[str, object]:
    """Read summarized sales for one period."""
    return {"period": period, "revenue": 1_240_000, "growth": "12.4%"}


async def main() -> None:
    sales_skill = Skill(
        name="sales-analysis",
        playbook="Use get_sales and preserve the returned figures.",
        contributions=SkillContributions(tools=(get_sales,)),
        permissions=SkillPermissions(
            required=frozenset({"sales:read"})
        ),
    )
    query_agent = Agent(
        AgentDefinition(
            id="sales-query",
            version="1.0.0",
            description="Retrieves trusted sales figures.",
            instructions="Return a compact data handoff.",
            skills=("sales-analysis",),
            permissions=frozenset({"sales:read"}),
        ),
        model=FakeModel(
            [
                ModelResponse(
                    tool_calls=(
                        ToolCall(
                            id="sales-1",
                            name="get_sales",
                            arguments={"period": "2026-Q2"},
                        ),
                    )
                ),
                ModelResponse(content="2026-Q2 revenue=1240000 growth=12.4%"),
            ]
        ),
        skills=(sales_skill,),
    )
    report_agent = Agent(
        AgentDefinition(
            id="report-writer",
            version="1.0.0",
            description="Turns verified figures into a concise report.",
            instructions="Use only the supplied figures.",
        ),
        model=FakeModel.text("2026-Q2 revenue grew 12.4% to 1.24M."),
    )

    flow = Flow.sequence(
        id="sales-report",
        stages=(
            Stage(name="query", agent=query_agent),
            Stage(
                name="report",
                agent=report_agent,
                prompt="Write a one-sentence report.",
                input_from="query",
            ),
        ),
    )
    result = await flow.run("Analyze 2026-Q2 sales")
    print(result.output)


if __name__ == "__main__":
    asyncio.run(main())
