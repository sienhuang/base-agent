"""Provider-neutral execution plans for orchestration strategies."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class PlanStatus(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    WAITING = "waiting"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class StepStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING = "waiting"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class InvalidPlanTransitionError(ValueError):
    """Raised when a plan step is moved through an invalid lifecycle transition."""


class PlanStep(BaseModel):
    """One immutable unit of work in an execution plan."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")
    description: str = Field(min_length=1)
    executor: str | None = None
    dependencies: tuple[str, ...] = ()
    status: StepStatus = StepStatus.PENDING
    result: Any | None = None
    error: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_dependencies_and_failure(self) -> PlanStep:
        if len(set(self.dependencies)) != len(self.dependencies):
            raise ValueError("step dependencies must be unique")
        if self.id in self.dependencies:
            raise ValueError("a step cannot depend on itself")
        if self.status is StepStatus.FAILED and not self.error:
            raise ValueError("a failed step must include an error")
        return self


class ExecutionPlan(BaseModel):
    """An immutable dependency graph that strategies may evolve by replacement."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")
    title: str = Field(min_length=1)
    steps: tuple[PlanStep, ...] = Field(min_length=1)
    status: PlanStatus = PlanStatus.CREATED
    revision: int = Field(default=1, ge=1)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_graph(self) -> ExecutionPlan:
        step_ids = [step.id for step in self.steps]
        if len(set(step_ids)) != len(step_ids):
            raise ValueError("plan step ids must be unique")
        known = set(step_ids)
        for step in self.steps:
            missing = set(step.dependencies) - known
            if missing:
                raise ValueError(
                    f"step '{step.id}' has unknown dependencies: {', '.join(sorted(missing))}"
                )
        self._assert_acyclic()
        return self

    def _assert_acyclic(self) -> None:
        dependencies = {step.id: step.dependencies for step in self.steps}
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(step_id: str) -> None:
            if step_id in visiting:
                raise ValueError("plan dependencies must not contain a cycle")
            if step_id in visited:
                return
            visiting.add(step_id)
            for dependency in dependencies[step_id]:
                visit(dependency)
            visiting.remove(step_id)
            visited.add(step_id)

        for step_id in dependencies:
            visit(step_id)

    def ready_steps(self) -> tuple[PlanStep, ...]:
        """Return pending steps whose dependencies have successfully settled."""

        statuses = {step.id: step.status for step in self.steps}
        settled = {StepStatus.COMPLETED, StepStatus.SKIPPED}
        return tuple(
            step
            for step in self.steps
            if step.status is StepStatus.PENDING
            and all(statuses[dependency] in settled for dependency in step.dependencies)
        )

    def start_step(self, step_id: str) -> ExecutionPlan:
        step = self._get_step(step_id)
        if step not in self.ready_steps():
            raise InvalidPlanTransitionError(f"step '{step_id}' is not ready to start")
        return self._replace_step(
            step.model_copy(update={"status": StepStatus.RUNNING}),
            plan_status=PlanStatus.RUNNING,
        )

    def complete_step(self, step_id: str, result: Any | None = None) -> ExecutionPlan:
        step = self._require_status(step_id, StepStatus.RUNNING)
        updated = self._replace_step(
            step.model_copy(
                update={"status": StepStatus.COMPLETED, "result": result, "error": None}
            )
        )
        if all(
            item.status in {StepStatus.COMPLETED, StepStatus.SKIPPED}
            for item in updated.steps
        ):
            return updated.model_copy(update={"status": PlanStatus.COMPLETED})
        return updated

    def fail_step(self, step_id: str, error: str) -> ExecutionPlan:
        if not error.strip():
            raise ValueError("error must not be empty")
        step = self._require_status(step_id, StepStatus.RUNNING)
        return self._replace_step(
            step.model_copy(update={"status": StepStatus.FAILED, "error": error}),
            plan_status=PlanStatus.FAILED,
        )

    def wait_step(self, step_id: str) -> ExecutionPlan:
        step = self._require_status(step_id, StepStatus.RUNNING)
        return self._replace_step(
            step.model_copy(update={"status": StepStatus.WAITING}),
            plan_status=PlanStatus.WAITING,
        )

    def resume_step(self, step_id: str) -> ExecutionPlan:
        step = self._require_status(step_id, StepStatus.WAITING)
        return self._replace_step(
            step.model_copy(update={"status": StepStatus.RUNNING}),
            plan_status=PlanStatus.RUNNING,
        )

    def _get_step(self, step_id: str) -> PlanStep:
        for step in self.steps:
            if step.id == step_id:
                return step
        raise KeyError(f"unknown plan step '{step_id}'")

    def _require_status(self, step_id: str, expected: StepStatus) -> PlanStep:
        step = self._get_step(step_id)
        if step.status is not expected:
            raise InvalidPlanTransitionError(
                f"step '{step_id}' must be {expected.value}, got {step.status.value}"
            )
        return step

    def _replace_step(
        self,
        replacement: PlanStep,
        *,
        plan_status: PlanStatus | None = None,
    ) -> ExecutionPlan:
        steps = tuple(
            replacement if step.id == replacement.id else step for step in self.steps
        )
        return self.model_copy(
            update={
                "steps": steps,
                "status": plan_status or self.status,
                "revision": self.revision + 1,
            }
        )
