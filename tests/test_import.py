import base_agent
from base_agent import Message, ModelProvider, ModelRequest, ModelResponse


def test_package_is_importable() -> None:
    assert base_agent.__version__ == "0.1.0"
    assert Message is not None
    assert ModelRequest is not None
    assert ModelResponse is not None
    assert ModelProvider is not None
