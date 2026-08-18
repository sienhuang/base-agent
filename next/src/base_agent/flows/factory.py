"""Small construction namespace for built-in Flow strategies."""

from collections.abc import Sequence

from base_agent.agent import Agent
from base_agent.artifacts import ArtifactRef, ArtifactSpec
from base_agent.events import EventStore
from base_agent.flows.planning import PlannerReActFlow, PlanningLimits
from base_agent.flows.sequential import SequentialFlow, Stage
from base_agent.observability import Observer


class Flow:
    """Construct a Flow while keeping concrete lifecycle types available."""

    @staticmethod
    def sequence(
        *,
        id: str,
        stages: Sequence[Stage],
        observers: Sequence[Observer] = (),
        event_store: EventStore | None = None,
        forward_child_events: bool = False,
        artifact_specs: Sequence[ArtifactSpec | ArtifactRef] = (),
        artifacts: Sequence[ArtifactSpec | ArtifactRef] = (),
    ) -> SequentialFlow:
        return SequentialFlow(
            id=id,
            stages=stages,
            observers=observers,
            event_store=event_store,
            forward_child_events=forward_child_events,
            artifact_specs=artifact_specs,
            artifacts=artifacts,
        )

    @staticmethod
    def planner_react(
        *,
        id: str,
        planner: Agent,
        executor: Agent,
        limits: PlanningLimits | None = None,
        observers: Sequence[Observer] = (),
        event_store: EventStore | None = None,
        forward_child_events: bool = False,
    ) -> PlannerReActFlow:
        return PlannerReActFlow(
            id=id,
            planner=planner,
            executor=executor,
            limits=limits,
            observers=observers,
            event_store=event_store,
            forward_child_events=forward_child_events,
        )
