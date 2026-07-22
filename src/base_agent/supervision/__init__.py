"""Composable supervision policies for bounded and recoverable execution."""

from base_agent.supervision.composite import CompositeSupervisor
from base_agent.supervision.decision import SupervisionAction, SupervisionDecision
from base_agent.supervision.policies import (
    DuplicateToolCallDetector,
    ExecutionBudget,
    NoProgressDetector,
    build_default_supervisor,
)
from base_agent.supervision.protocol import BaseSupervisor, Supervisor

__all__ = [
    "BaseSupervisor",
    "CompositeSupervisor",
    "DuplicateToolCallDetector",
    "ExecutionBudget",
    "NoProgressDetector",
    "SupervisionAction",
    "SupervisionDecision",
    "Supervisor",
    "build_default_supervisor",
]
