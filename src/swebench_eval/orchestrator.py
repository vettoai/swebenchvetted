"""Parallel execution coordinator with event bus."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from swebench_eval.docker_builder import build_image
from swebench_eval.evaluator import evaluate_task
from swebench_eval.litellm_proxy import LiteLLMProxy
from swebench_eval.models import (
    ModelEvaluation,
    Task,
    TaskEvaluation,
    TaskStatus,
    pass_at_k,
)

logger = logging.getLogger(__name__)


class EventType(str, Enum):
    build_start = "build_start"
    build_done = "build_done"
    task_status = "task_status"
    task_done = "task_done"
    all_done = "all_done"


@dataclass
class Event:
    type: EventType
    task_id: str = ""
    data: dict[str, Any] = field(default_factory=dict)


class EvaluationOrchestrator:
    """Coordinates parallel image builds and task evaluations."""

    def __init__(
        self,
        tasks: list[Task],
        *,
        model: str,
        api_base: str | None = None,
        n_attempts: int = 3,
        max_concurrent: int = 4,
        timeout_seconds: int = 1800,
        force_rebuild: bool = False,
        stagger_seconds: int = 0,
        on_event: Callable[[Event], None] | None = None,
    ) -> None:
        self.tasks = tasks
        self.model = model
        self.api_base = api_base
        self.n_attempts = n_attempts
        self.max_concurrent = max_concurrent
        self.timeout_seconds = timeout_seconds
        self.force_rebuild = force_rebuild
        self.stagger_seconds = stagger_seconds
        self.on_event = on_event
        self._image_tags: dict[str, str] = {}
        self._proxy: LiteLLMProxy | None = None

    def _emit(self, event: Event) -> None:
        if self.on_event:
            self.on_event(event)

    async def run(self) -> ModelEvaluation:
        """Execute the full evaluation pipeline."""
        sem = asyncio.Semaphore(self.max_concurrent)

        # Phase 1: Build all Docker images
        logger.info("Phase 1: Building %d Docker images", len(self.tasks))
        build_results = await asyncio.gather(
            *(self._build_one(task, sem) for task in self.tasks)
        )

        # Filter to successfully built tasks
        runnable: list[Task] = []
        for task, result in zip(self.tasks, build_results):
            if result.success:
                self._image_tags[task.metadata.id] = result.tag
                runnable.append(task)
            else:
                self._emit(Event(
                    type=EventType.task_status,
                    task_id=task.metadata.id,
                    data={"status": TaskStatus.failed, "error": result.error},
                ))

        # Phase 2: Start LiteLLM proxy
        logger.info("Phase 2: Starting LiteLLM proxy for model %s", self.model)
        async with LiteLLMProxy(self.model, api_base=self.api_base, num_workers=self.max_concurrent) as proxy:
            self._proxy = proxy
            # Phase 3: Evaluate tasks in parallel
            logger.info("Phase 3: Evaluating %d tasks", len(runnable))
            evaluations = await asyncio.gather(
                *(self._evaluate_one(task, sem, i) for i, task in enumerate(runnable))
            )
            self._proxy = None

        # Phase 4: Aggregate results
        logger.info("Phase 4: Aggregating results")
        model_eval = self._aggregate(evaluations)
        self._emit(Event(type=EventType.all_done, data={
            "pass_at_1": model_eval.pass_at_1,
            "pass_at_3": model_eval.pass_at_3,
            "resolved_rate": model_eval.resolved_rate,
        }))
        return model_eval

    async def _build_one(self, task: Task, sem: asyncio.Semaphore) -> Any:
        async with sem:
            self._emit(Event(type=EventType.build_start, task_id=task.metadata.id))
            result = await build_image(task, force_rebuild=self.force_rebuild)
            self._emit(Event(
                type=EventType.build_done,
                task_id=task.metadata.id,
                data={"success": result.success, "time": result.build_time},
            ))
            return result

    async def _evaluate_one(self, task: Task, sem: asyncio.Semaphore, index: int) -> TaskEvaluation:
        if self.stagger_seconds > 0 and index > 0:
            await asyncio.sleep(index * self.stagger_seconds)
        async with sem:
            def on_status(task_id: str, status: TaskStatus, data: dict[str, Any]) -> None:
                self._emit(Event(type=EventType.task_status, task_id=task_id, data={"status": status, **data}))

            result = await evaluate_task(
                task,
                image_tag=self._image_tags[task.metadata.id],
                n_attempts=self.n_attempts,
                timeout_seconds=self.timeout_seconds,
                on_status=on_status,
                proxy=self._proxy,
            )
            self._emit(Event(
                type=EventType.task_done,
                task_id=task.metadata.id,
                data={"resolved_count": result.resolved_count},
            ))
            return result

    def _aggregate(self, evaluations: list[TaskEvaluation]) -> ModelEvaluation:
        n = self.n_attempts
        pass_1_values: list[float] = []
        pass_3_values: list[float] = []
        resolved_tasks = 0

        for te in evaluations:
            c = te.resolved_count
            pass_1_values.append(pass_at_k(n, c, 1))
            pass_3_values.append(pass_at_k(n, c, min(3, n)))
            if c > 0:
                resolved_tasks += 1

        total = len(evaluations) if evaluations else 1
        return ModelEvaluation(
            model=self.model,
            n_attempts=n,
            evaluations=evaluations,
            pass_at_1=sum(pass_1_values) / total if pass_1_values else 0.0,
            pass_at_3=sum(pass_3_values) / total if pass_3_values else 0.0,
            resolved_rate=resolved_tasks / total,
        )
