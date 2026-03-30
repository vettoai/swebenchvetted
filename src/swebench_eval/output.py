"""Results writing: JSON summaries and per-task logs."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from swebench_eval.models import ModelEvaluation

logger = logging.getLogger(__name__)


def write_results(evaluation: ModelEvaluation, output_dir: Path) -> Path:
    """Write evaluation results to ``output_dir/run_<timestamp>/``.

    Returns the run directory path.
    """
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    run_dir = output_dir / f"run_{timestamp}"
    tasks_dir = run_dir / "tasks"
    logs_dir = run_dir / "logs"
    tasks_dir.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)

    # Summary
    summary_path = run_dir / "summary.json"
    summary_path.write_text(
        evaluation.model_dump_json(by_alias=True, indent=2) + "\n"
    )
    logger.info("Wrote summary to %s", summary_path)

    # Per-task evaluations + logs
    for task_eval in evaluation.evaluations:
        tid = task_eval.task_id

        # Task JSON
        task_json_path = tasks_dir / f"{tid}.json"
        task_json_path.write_text(
            task_eval.model_dump_json(by_alias=True, indent=2) + "\n"
        )

        # Per-attempt logs
        task_log_dir = logs_dir / tid
        task_log_dir.mkdir(parents=True, exist_ok=True)
        for attempt in task_eval.attempts:
            n = attempt.attempt
            (task_log_dir / f"attempt_{n}_agent.log").write_text(attempt.agent_log)
            (task_log_dir / f"attempt_{n}_scoring.log").write_text(attempt.scoring_log)
            (task_log_dir / f"attempt_{n}_patch.diff").write_text(attempt.agent_patch)

    logger.info("Wrote %d task results to %s", len(evaluation.evaluations), run_dir)
    return run_dir


def load_results(run_dir: Path) -> ModelEvaluation:
    """Load a ModelEvaluation from a run directory's summary.json."""
    summary_path = run_dir / "summary.json"
    data = json.loads(summary_path.read_text())
    return ModelEvaluation.model_validate(data)
