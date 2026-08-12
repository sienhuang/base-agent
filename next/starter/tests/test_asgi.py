from agent_app.asgi import app, create_app


def test_starter_composes_sdk_http_adapter() -> None:
    built = create_app()

    assert app.title == "base-agent Starter"
    assert built.title == "base-agent Starter"
    assert {route.path for route in built.routes} >= {
        "/v1/runs",
        "/v1/runs/{run_id}",
        "/v1/runs/{run_id}/events",
        "/v1/runs/{run_id}/events/stream",
    }
    assert callable(built.state.run_registry.get)
