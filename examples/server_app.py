"""Offline FastAPI example: uv run --extra server uvicorn examples.server_app:app."""

from base_agent import Agent, AgentProfile, ModelRequest, ModelResponse
from base_agent.server import create_app


class EchoModel:
    name = "offline-echo"

    async def complete(self, request: ModelRequest) -> ModelResponse:
        prompt = request.messages[-1].content or ""
        return ModelResponse(content=f"Echo: {prompt}")


agent = Agent(
    profile=AgentProfile(id="server-example", instructions="Echo the user's request."),
    model=EchoModel(),
)
app = create_app(agent)
