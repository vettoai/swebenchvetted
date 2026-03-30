"""Data models for the SWE-bench evaluation harness."""

from __future__ import annotations

import math
from enum import Enum

from pydantic import BaseModel, ConfigDict, computed_field
from pydantic.alias_generators import to_camel

_CAMEL_CONFIG = ConfigDict(
    frozen=True,
    alias_generator=to_camel,
    populate_by_name=True,
)


# ---------------------------------------------------------------------------
# Task input models (matches cloud pipeline JSON schema)
# ---------------------------------------------------------------------------


class TestFile(BaseModel):
    """A generated test file with source code.

    ``patch`` is computed on the fly as a ``git apply``-ready unified diff
    that creates the file from scratch.
    """

    model_config = _CAMEL_CONFIG
    file_path: str
    test_name: str
    content: str

    @computed_field  # type: ignore[prop-decorator]
    @property
    def patch(self) -> str:
        """Build a unified diff that creates this file from /dev/null."""
        lines = self.content.split("\n")
        diff_lines = [
            f"diff --git a/{self.file_path} b/{self.file_path}",
            "new file mode 100644",
            "--- /dev/null",
            f"+++ b/{self.file_path}",
            f"@@ -0,0 +1,{len(lines)} @@",
        ]
        for line in lines:
            diff_lines.append(f"+{line}")
        return "\n".join(diff_lines) + "\n"


class ExistingTestDeletion(BaseModel):
    """LLM-generated deletion patch for an existing test file."""

    model_config = _CAMEL_CONFIG
    file_path: str
    test_names: list[str]
    patch: str


class TaskIssue(BaseModel):
    model_config = _CAMEL_CONFIG
    description: str


class TaskVerifier(BaseModel):
    model_config = _CAMEL_CONFIG
    added_tests: list[TestFile]
    deleted_tests: list[ExistingTestDeletion]
    expected: dict[str, str]


class TaskEnvironment(BaseModel):
    model_config = _CAMEL_CONFIG
    image_name: str
    dockerfile: str
    context_files: dict[str, str]


class TaskMetadata(BaseModel):
    model_config = _CAMEL_CONFIG
    id: str
    source: str
    language: str


class Task(BaseModel):
    model_config = _CAMEL_CONFIG
    metadata: TaskMetadata
    issue: TaskIssue
    verifier: TaskVerifier
    environment: TaskEnvironment


# ---------------------------------------------------------------------------
# Result models
# ---------------------------------------------------------------------------


class EvaluationAttempt(BaseModel):
    """Result of a single evaluation attempt for one task."""

    model_config = _CAMEL_CONFIG
    attempt: int
    agent_patch: str
    test_results: dict[str, str]
    resolved: bool
    agent_log: str
    agent_trajectory: str = ""
    scoring_exit_code: int | None = None
    scoring_log: str = ""


class TaskEvaluation(BaseModel):
    """All attempts for a single task against a single model."""

    model_config = _CAMEL_CONFIG
    task_id: str
    attempts: list[EvaluationAttempt]
    resolved_count: int


class ModelEvaluation(BaseModel):
    """Aggregated results for all tasks evaluated against one model."""

    model_config = _CAMEL_CONFIG
    model: str
    n_attempts: int
    evaluations: list[TaskEvaluation]
    pass_at_1: float
    pass_at_3: float
    resolved_rate: float


# ---------------------------------------------------------------------------
# TUI status enum
# ---------------------------------------------------------------------------


class TaskStatus(str, Enum):
    pending = "pending"
    building = "building"
    agent_running = "agent_running"
    scoring = "scoring"
    completed = "completed"
    failed = "failed"


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def pass_at_k(n: int, c: int, k: int) -> float:
    """Unbiased estimator: probability at least 1 of *k* random samples is correct.

    Uses the standard estimator from the Codex paper (Chen et al., 2021).

    Args:
        n: Total number of samples generated.
        c: Number of correct (resolved) samples.
        k: Number of samples drawn.
    """
    if n - c < k:
        return 1.0
    return 1.0 - math.prod((n - c - i) / (n - i) for i in range(k))
