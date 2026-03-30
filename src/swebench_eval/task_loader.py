"""Load Task objects from JSON/JSONL files or directories."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from swebench_eval.models import Task

logger = logging.getLogger(__name__)


def load_tasks(path: Path, *, filter_prefix: str | None = None) -> list[Task]:
    """Load tasks from a file or directory.

    - File: tries JSON array first, then single object, then JSONL.
    - Directory: globs ``*.json`` + ``*.jsonl`` and loads all.
    """
    if path.is_dir():
        tasks: list[Task] = []
        for pattern in ("*.json", "*.jsonl"):
            for child in sorted(path.glob(pattern)):
                tasks.extend(_load_file(child))
    else:
        tasks = _load_file(path)

    if filter_prefix:
        tasks = [t for t in tasks if t.metadata.id.startswith(filter_prefix)]

    logger.info("Loaded %d tasks%s", len(tasks), f" (filter={filter_prefix!r})" if filter_prefix else "")
    return tasks


def _load_file(path: Path) -> list[Task]:
    """Load tasks from a single file, auto-detecting format."""
    text = path.read_text()

    # Try JSON array or single object
    try:
        data = json.loads(text)
        if isinstance(data, list):
            return [Task.model_validate(item) for item in data]
        if isinstance(data, dict):
            return [Task.model_validate(data)]
    except json.JSONDecodeError:
        pass

    # Fall back to JSONL
    tasks: list[Task] = []
    for line_no, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            tasks.append(Task.model_validate(json.loads(line)))
        except Exception as exc:
            logger.warning("Skipping %s line %d: %s", path, line_no, exc)
    return tasks
