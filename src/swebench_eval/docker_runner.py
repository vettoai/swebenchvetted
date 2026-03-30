"""Run containers locally, replacing the K8s runner."""

from __future__ import annotations

import asyncio
import logging
import tempfile
from dataclasses import dataclass
from pathlib import Path

import docker

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ContainerResult:
    exit_code: int
    stdout: str


def _run_sync(
    image: str,
    command: list[str],
    *,
    timeout_seconds: int = 1800,
    files: dict[str, str] | None = None,
    env: dict[str, str] | None = None,
    network_mode: str = "host",
) -> ContainerResult:
    """Run a container and return its output (blocking)."""
    client = docker.from_env()
    volumes: dict[str, dict[str, str]] = {}
    tmp_dir = None

    try:
        # File injection: write files to a temp dir and bind-mount them
        if files:
            tmp_dir = tempfile.mkdtemp(prefix="swebench-eval-")
            for container_path, content in files.items():
                # Flatten the container path into the temp dir
                safe_name = container_path.lstrip("/").replace("/", "__")
                host_path = Path(tmp_dir) / safe_name
                host_path.write_text(content)
                volumes[str(host_path)] = {"bind": container_path, "mode": "ro"}

        container = client.containers.run(
            image,
            command=command,
            environment=env or {},
            volumes=volumes,
            network_mode=network_mode,
            detach=True,
            stdout=True,
            stderr=True,
        )

        try:
            result = container.wait(timeout=timeout_seconds)
            exit_code = result.get("StatusCode", -1)
        except Exception:
            logger.warning("Container %s timed out after %ds, killing", container.short_id, timeout_seconds)
            container.kill()
            exit_code = -1

        stdout = container.logs(stdout=True, stderr=True).decode("utf-8", errors="replace")

        return ContainerResult(exit_code=exit_code, stdout=stdout)
    finally:
        # Always clean up container
        try:
            container.remove(force=True)  # type: ignore[possibly-undefined]
        except Exception:
            pass
        # Clean up temp files
        if tmp_dir:
            import shutil

            shutil.rmtree(tmp_dir, ignore_errors=True)


async def run_container(
    image: str,
    command: list[str],
    *,
    timeout_seconds: int = 1800,
    files: dict[str, str] | None = None,
    env: dict[str, str] | None = None,
    network_mode: str = "host",
) -> ContainerResult:
    """Run a container asynchronously."""
    return await asyncio.to_thread(
        _run_sync,
        image,
        command,
        timeout_seconds=timeout_seconds,
        files=files,
        env=env,
        network_mode=network_mode,
    )
