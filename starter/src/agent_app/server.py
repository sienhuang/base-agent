"""Optional FastAPI application: install the starter's server extra first."""

from base_agent.server import create_app

from agent_app.agent import build_agent

app = create_app(build_agent())
