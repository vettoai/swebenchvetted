"""Single-task evaluation logic: agent run + scoring run."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from swebench_eval.docker_runner import run_container
from swebench_eval.models import (
    EvaluationAttempt,
    Task,
    TaskEvaluation,
    TaskStatus,
)
from swebench_eval.scoring import (
    build_combined_test_patch,
    check_expected,
    parse_agent_patch,
    parse_test_output,
)

logger = logging.getLogger(__name__)

_MAX_LOG_CHARS = 8000


async def evaluate_task(
    task: Task,
    *,
    image_tag: str,
    n_attempts: int = 3,
    timeout_seconds: int = 1800,
    on_status: Callable[[str, TaskStatus, dict[str, Any]], None] | None = None,
) -> TaskEvaluation:
    """Run a coding agent against *task* for *n_attempts* and score results.

    Each attempt consists of two container runs:

    1. **Agent run**: Runs the evaluate.sh script inside the task image with
       the LiteLLM proxy reachable at localhost:10000 via --network host.
    2. **Scoring run**: Applies the agent's patch plus the test patch, then
       runs the verifier to check pass/fail status.
    """
    task_id = task.metadata.id
    attempts: list[EvaluationAttempt] = []
    test_patch = build_combined_test_patch(task.verifier.added_tests)

    def emit(status: TaskStatus, **extra: Any) -> None:
        if on_status:
            on_status(task_id, status, extra)

    for attempt_idx in range(n_attempts):
        logger.info("Task %s attempt %d/%d: running agent", task_id, attempt_idx + 1, n_attempts)
        emit(TaskStatus.agent_running, attempt=attempt_idx + 1)

        # --- Agent run ---
        try:
            agent_result = await run_container(
                image_tag,
                command=["/testbed/evaluate.sh"],
                timeout_seconds=timeout_seconds,
                files={"/testbed/problem_statement.txt": task.issue.description},
                env={"OPENAI_API_KEY": "dummy"},
                network_mode="host",
            )
        except Exception:
            logger.warning("Task %s attempt %d: agent run failed", task_id, attempt_idx + 1, exc_info=True)
            attempts.append(
                EvaluationAttempt(
                    attempt=attempt_idx,
                    agent_patch="",
                    test_results={},
                    resolved=False,
                    agent_log="Agent run failed with exception",
                )
            )
            continue

        agent_patch = parse_agent_patch(agent_result.stdout)
        agent_log = agent_result.stdout[-_MAX_LOG_CHARS:]
        agent_trajectory = agent_result.stdout

        logger.info(
            "Task %s attempt %d: extracted patch (%d chars)",
            task_id, attempt_idx + 1, len(agent_patch),
        )

        if not agent_patch:
            logger.warning("Task %s attempt %d: agent produced no patch", task_id, attempt_idx + 1)
            attempts.append(
                EvaluationAttempt(
                    attempt=attempt_idx,
                    agent_patch="",
                    test_results={},
                    resolved=False,
                    agent_log=agent_log,
                    agent_trajectory=agent_trajectory,
                )
            )
            continue

        # --- Scoring run ---
        logger.info("Task %s attempt %d: scoring", task_id, attempt_idx + 1)
        emit(TaskStatus.scoring, attempt=attempt_idx + 1)

        try:
            score_result = await run_container(
                image_tag,
                command=["/testbed/verify_solution"],
                timeout_seconds=900,
                files={
                    "/test.patch": test_patch,
                    "/solution.patch": agent_patch,
                },
            )
        except Exception as exc:
            logger.warning("Task %s attempt %d: scoring failed", task_id, attempt_idx + 1, exc_info=True)
            attempts.append(
                EvaluationAttempt(
                    attempt=attempt_idx,
                    agent_patch=agent_patch,
                    test_results={},
                    resolved=False,
                    agent_log=agent_log,
                    agent_trajectory=agent_trajectory,
                    scoring_exit_code=-1,
                    scoring_log=f"Scoring container exception: {exc}",
                )
            )
            continue

        scoring_log = score_result.stdout[-_MAX_LOG_CHARS:]
        test_results = parse_test_output(score_result.stdout)
        resolved = check_expected(test_results, task.verifier.expected)

        if score_result.exit_code != 0:
            logger.warning(
                "Task %s attempt %d: scoring exited with code %d",
                task_id, attempt_idx + 1, score_result.exit_code,
            )

        logger.info(
            "Task %s attempt %d: exit_code=%d resolved=%s (%d test results)",
            task_id, attempt_idx + 1, score_result.exit_code, resolved, len(test_results),
        )
        attempts.append(
            EvaluationAttempt(
                attempt=attempt_idx,
                agent_patch=agent_patch,
                test_results=test_results,
                resolved=resolved,
                agent_log=agent_log,
                agent_trajectory=agent_trajectory,
                scoring_exit_code=score_result.exit_code,
                scoring_log=scoring_log,
            )
        )

    resolved_count = sum(1 for a in attempts if a.resolved)
    emit(TaskStatus.completed, resolved_count=resolved_count)

    return TaskEvaluation(
        task_id=task_id,
        attempts=attempts,
        resolved_count=resolved_count,
    )
