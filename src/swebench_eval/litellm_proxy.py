"""Managed LiteLLM subprocess lifecycle."""

from __future__ import annotations

import asyncio
import logging
import signal
import subprocess

import httpx

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
    ) -> None:
        self.model = model
        self.api_base = api_base
        self.port = port
        self.num_workers = num_workers
        self._process: subprocess.Popen[bytes] | None = None

    async def __aenter__(self) -> LiteLLMProxy:
        cmd: list[str] = [
            "litellm",
            "--model", self.model,
            "--port", str(self.port),
            "--num_workers", str(self.num_workers),
        ]
        if self.api_base:
            cmd.extend(["--api_base", self.api_base])

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
