from base_agent import (
    Agent,
    AgentDefinition,
    Flow,
    FlowStatus,
    Stage,
    VerificationEvidence,
)
from base_agent.testing import FakeModel


async def test_sequential_flow_passes_previous_stage_output_to_the_next_agent() -> None:
    query_model = FakeModel.text("revenue=1240000,growth=12.4%")
    analysis_model = FakeModel.text("Revenue grew 12.4% to 1.24M.")
    query_agent = Agent(
        AgentDefinition(
            id="query-agent",
            version="1.0.0",
            instructions="Fetch data.",
        ),
        model=query_model,
    )
    analysis_agent = Agent(
        AgentDefinition(
            id="analysis-agent",
            version="1.0.0",
            instructions="Analyze data.",
        ),
        model=analysis_model,
    )
    flow = Flow.sequence(
        id="analysis",
        stages=(
            Stage(
                name="query",
                agent=query_agent,
                prompt="Create a compact dataset.",
            ),
            Stage(
                name="analysis",
                agent=analysis_agent,
                prompt="Explain the following dataset.",
                input_from="query",
                verify=lambda run: VerificationEvidence(
                    verifier="report_non_empty",
                    scope="stage_output",
                    status="passed" if bool(run.text.strip()) else "failed",
                ),
            ),
        ),
    )

    result = await flow.run("Analyze Q2 revenue")

    assert result.status is FlowStatus.SUCCEEDED
    assert result.output == "Revenue grew 12.4% to 1.24M."
    assert (
        analysis_model.requests[0].messages[0].content
        == "Explain the following dataset.\n\nInput:\nrevenue=1240000,growth=12.4%"
    )
    assert [event.kind for event in result.events] == [
        "flow.started",
        "stage.started",
        "stage.succeeded",
        "stage.started",
        "stage.succeeded",
        "flow.succeeded",
    ]
    assert result.stages[1].evidence[0].status == "passed"
