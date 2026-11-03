"""Test doubles that preserve the DSH adapter boundary."""

from dsh_base_agent.testing.fake_runtime import (
    RecordedRuntimeCall,
    ScriptedRuntimeFactory,
)

__all__ = ["RecordedRuntimeCall", "ScriptedRuntimeFactory"]
