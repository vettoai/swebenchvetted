"""Managed LiteLLM subprocess lifecycle."""

from __future__ import annotations

import asyncio
import logging
import signal
import subprocess
import tempfile
from pathlib import Path

import httpx
import yaml

logger = logging.getLogger(__name__)

_HEALTH_URL = "http://localhost:{port}/health/liveliness"
_HEALTH_POLL_INTERVAL = 2.0
_HEALTH_TIMEOUT = 120.0
_SHUTDOWN_GRACE = 10


class LiteLLMProxy:
    """Async context manager that runs a LiteLLM proxy subprocess on a given port.

    API keys are passed through from the environment (OPENAI_API_KEY,
    ANTHROPIC_API_KEY, GEMINI_API_KEY — LiteLLM reads these automatically).
    """

    def __init__(
        self,
        model: str,
        *,
        api_base: str | None = None,
        port: int = 10000,
        num_workers: int = 4,
        num_retries: int = 10,
        retry_after: int = 5,
    ) -> None:
        self.model = model
        self.api_base = api_base
        self.port = port
        self.num_workers = num_workers
        self.num_retries = num_retries
        self.retry_after = retry_after
        self._process: subprocess.Popen[bytes] | None = None
        self._config_dir: tempfile.TemporaryDirectory[str] | None = None

    def _write_config(self) -> Path:
        """Generate a LiteLLM YAML config with retry settings."""
        litellm_params: dict[str, object] = {"model": self.model}
        if self.api_base:
            litellm_params["api_base"] = self.api_base

        config = {
            "model_list": [
                {
                    "model_name": "eval-model",
                    "litellm_params": litellm_params,
                }
            ],
            "litellm_settings": {
                "num_retries": self.num_retries,
                "retry_after": self.retry_after,
            },
        }

        self._config_dir = tempfile.TemporaryDirectory(prefix="litellm-config-")
        config_path = Path(self._config_dir.name) / "config.yaml"
        config_path.write_text(yaml.dump(config, default_flow_style=False))
        return config_path

    async def __aenter__(self) -> LiteLLMProxy:
        config_path = self._write_config()
        cmd: list[str] = [
            "litellm",
            "--config", str(config_path),
            "--port", str(self.port),
            "--num_workers", str(self.num_workers),
        ]

        logger.info("Starting LiteLLM proxy: %s", " ".join(cmd))
        self._process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        await self._wait_healthy()
        # Discard stdout after healthy to avoid blocking on the pipe
        self._process.stdout.close()  # type: ignore[union-attr]
        logger.info("LiteLLM proxy is healthy on port %d", self.port)
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._process is None:
            return
        logger.info("Stopping LiteLLM proxy (pid=%d)", self._process.pid)
        self._process.send_signal(signal.SIGTERM)
        try:
            await asyncio.to_thread(self._process.wait, timeout=_SHUTDOWN_GRACE)
        except subprocess.TimeoutExpired:
            logger.warning("LiteLLM proxy did not exit gracefully, sending SIGKILL")
            self._process.kill()
            await asyncio.to_thread(self._process.wait)
        self._process = None
        if self._config_dir:
            self._config_dir.cleanup()
            self._config_dir = None

    async def _wait_healthy(self) -> None:
        """Poll the health endpoint until it responds 200."""
        assert self._process is not None
        url = _HEALTH_URL.format(port=self.port)
        elapsed = 0.0
        async with httpx.AsyncClient() as client:
            while elapsed < _HEALTH_TIMEOUT:
                # Check if the process has crashed
                if self._process.poll() is not None:
                    # Read whatever output is available for diagnostics
                    out = ""
                    if self._process.stdout:
                        out = self._process.stdout.read().decode("utf-8", errors="replace")
                    raise RuntimeError(
                        f"LiteLLM proxy exited with code {self._process.returncode}:\n{out}"
                    )
                try:
                    resp = await client.get(url, timeout=5)
                    if resp.status_code == 200:
                        return
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(_HEALTH_POLL_INTERVAL)
                elapsed += _HEALTH_POLL_INTERVAL
        raise TimeoutError(f"LiteLLM proxy did not become healthy within {_HEALTH_TIMEOUT}s")
