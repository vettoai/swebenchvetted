"""Patch parsing, test output parsing, and metrics."""

from __future__ import annotations

import re

from swebench_eval.models import TestFile

_AGENT_PATCH_RE = re.compile(
    r"=== AGENT_PATCH ===\n(.*?\n)=== END_AGENT_PATCH ===",
    re.DOTALL,
)

FAILURE_DETAILS_MARKER = "--- FAILURE DETAILS ---"


def parse_agent_patch(stdout: str) -> str:
    """Extract the git diff between the ``=== AGENT_PATCH ===`` markers."""
    match = _AGENT_PATCH_RE.search(stdout)
    if not match:
        return ""
    patch = match.group(1)
    if not patch.strip():
        return ""
    if not patch.endswith("\n"):
        patch += "\n"
    return patch


def parse_test_output(stdout: str) -> dict[str, str]:
    """Parse tab-separated ``test_name\\tSTATUS`` lines from verifier output."""
    results: dict[str, str] = {}
    for line in stdout.splitlines():
        if line.strip() == FAILURE_DETAILS_MARKER:
            break
        parts = line.split("\t")
        if len(parts) >= 2:
            results[parts[0]] = parts[1]
    return results


def check_expected(test_results: dict[str, str], expected: dict[str, str]) -> bool:
    """Return True if all expected test outcomes that were actually run match.

    Tests present in *expected* but absent from *test_results* are skipped
    because they were added by the gold patch (not the test patch) and won't
    exist in the agent's scoring environment.
    """
    present = {
        name: status
        for name, status in expected.items()
        if name in test_results
    }
    if not present:
        return False
    return all(test_results[name] == status for name, status in present.items())


def build_combined_test_patch(added_tests: list[TestFile]) -> str:
    """Concatenate all added-test patches into a single patch string."""
    patches: list[str] = []
    for test_file in added_tests:
        patches.append(test_file.patch)
    return "\n".join(patches)
