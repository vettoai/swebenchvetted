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

    The proxy auto-restarts if the process dies while the context manager is active.
    Call :meth:`ensure_healthy` before each attempt to trigger a restart if needed.
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
        self._config_path: Path | None = None
        self._cmd: list[str] = []

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

    def _start_process(self) -> None:
        """Start (or restart) the LiteLLM proxy subprocess."""
        logger.info("Starting LiteLLM proxy: %s", " ".join(self._cmd))
        self._process = subprocess.Popen(
            self._cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )

    def _stop_process(self) -> None:
        """Stop the proxy process if running."""
        if self._process is None:
            return
        try:
            if self._process.poll() is None:
                self._process.send_signal(signal.SIGTERM)
                try:
                    self._process.wait(timeout=_SHUTDOWN_GRACE)
                except subprocess.TimeoutExpired:
                    logger.warning("LiteLLM proxy did not exit gracefully, sending SIGKILL")
                    self._process.kill()
                    self._process.wait()
        except OSError:
            pass  # Process already dead
        self._process = None

    async def __aenter__(self) -> LiteLLMProxy:
        self._config_path = self._write_config()
        self._cmd = [
            "litellm",
            "--config", str(self._config_path),
            "--port", str(self.port),
            "--num_workers", str(self.num_workers),
        ]

        self._start_process()
        await self._wait_healthy()
        # Discard stdout after healthy to avoid blocking on the pipe
        if self._process and self._process.stdout:
            self._process.stdout.close()
        logger.info("LiteLLM proxy is healthy on port %d", self.port)
        return self

    async def __aexit__(self, *exc: object) -> None:
        self._stop_process()
        if self._config_dir:
            self._config_dir.cleanup()
            self._config_dir = None

    async def ensure_healthy(self) -> None:
        """Check proxy liveness; restart if the process has died."""
        if self._process is not None and self._process.poll() is None:
            # Process is still running — do a quick health check
            url = _HEALTH_URL.format(port=self.port)
            try:
                async with httpx.AsyncClient() as client:
                    resp = await client.get(url, timeout=5)
                    if resp.status_code == 200:
                        return
            except httpx.HTTPError:
                pass
            # Health check failed but process is alive — give it a moment
            logger.warning("LiteLLM proxy health check failed, waiting before retry")
            await asyncio.sleep(5)
            try:
                async with httpx.AsyncClient() as client:
                    resp = await client.get(url, timeout=5)
                    if resp.status_code == 200:
                        return
            except httpx.HTTPError:
                pass

        # Process is dead or unresponsive — restart
        logger.warning("LiteLLM proxy is down, restarting")
        self._stop_process()
        self._start_process()
        await self._wait_healthy()
        if self._process and self._process.stdout:
            self._process.stdout.close()
        logger.info("LiteLLM proxy restarted and healthy on port %d", self.port)

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