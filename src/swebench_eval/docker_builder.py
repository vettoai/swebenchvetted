"""Build Docker images locally from task dockerfile + context_files."""

from __future__ import annotations

import asyncio
import logging
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import docker

from swebench_eval.models import Task

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ImageBuildResult:
    tag: str
    success: bool
    build_time: float
    error: str = ""


def _tag_for_task(task: Task) -> str:
    return f"swebenchvetted-eval/{task.metadata.id}:latest".lower()


def _image_exists(client: docker.DockerClient, tag: str) -> bool:
    try:
        client.images.get(tag)
        return True
    except docker.errors.ImageNotFound:
        return False


def _build_sync(task: Task, *, force_rebuild: bool = False) -> ImageBuildResult:
    """Build the Docker image for a task (blocking)."""
    tag = _tag_for_task(task)
    client = docker.from_env()

    if not force_rebuild and _image_exists(client, tag):
        logger.info("Image %s already exists, skipping build", tag)
        return ImageBuildResult(tag=tag, success=True, build_time=0.0)

    start = time.monotonic()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            (tmp_path / "Dockerfile").write_text(task.environment.dockerfile)
            for name, content in task.environment.context_files.items():
                file_path = tmp_path / name
                file_path.parent.mkdir(parents=True, exist_ok=True)
                file_path.write_text(content)

            client.images.build(path=str(tmp_path), tag=tag, rm=True)

        elapsed = time.monotonic() - start
        logger.info("Built image %s in %.1fs", tag, elapsed)
        return ImageBuildResult(tag=tag, success=True, build_time=elapsed)
    except Exception as exc:
        elapsed = time.monotonic() - start
        logger.error("Failed to build image %s: %s", tag, exc)
        return ImageBuildResult(tag=tag, success=False, build_time=elapsed, error=str(exc))


async def build_image(task: Task, *, force_rebuild: bool = False) -> ImageBuildResult:
    """Build the Docker image for a task (async wrapper)."""
    return await asyncio.to_thread(_build_sync, task, force_rebuild=force_rebuild)
