"""Patch parsing, test output parsing, and metrics."""

from __future__ import annotations

import re
from pathlib import PurePosixPath

from swebench_eval.models import ExistingTestDeletion, TestFile

_AGENT_PATCH_RE = re.compile(
    r"=== AGENT_PATCH ===\n(.*?\n)=== END_AGENT_PATCH ===",
    re.DOTALL,
)

FAILURE_DETAILS_MARKER = "--- FAILURE DETAILS ---"

_PASS_STATUSES = frozenset({"PASSED", "SKIPPED", "XFAIL", "XPASS"})


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
        if len(parts) >= 2 and parts[0].strip():
            results[parts[0]] = parts[1]
    return results


def _build_generated_markers(added_tests: list[TestFile]) -> frozenset[str]:
    """Build a set of substrings that identify generated test IDs.

    Each ``TestFile`` contributes three markers:
    - ``file_path``  — matches Python test IDs (``tests/test_foo.py::test_func``)
    - ``Path(file_path).stem`` — matches Java test IDs (``TestFoo#testFunc``)
    - ``test_name`` — matches Go test IDs (bare function names)
    """
    markers: set[str] = set()
    for tf in added_tests:
        markers.add(tf.file_path)
        markers.add(PurePosixPath(tf.file_path).stem)
        markers.add(tf.test_name)
    return frozenset(markers)


def _is_generated_test(test_id: str, markers: frozenset[str]) -> bool:
    """Return True if *test_id* matches any generated-test marker."""
    return any(m in test_id for m in markers)


def check_resolved(
    test_results: dict[str, str],
    added_tests: list[TestFile],
    ignored: list[str],
) -> bool:
    """Heuristic scoring: generated tests must PASS, existing tests must not regress.

    1. Build markers from *added_tests* and partition *test_results* into
       generated vs existing.
    2. Skip any test whose ID is in *ignored*.
    3. All generated tests must have status ``"PASSED"``.
    4. No existing (non-ignored) test may have a status outside ``_PASS_STATUSES``.
    5. Return ``False`` if no generated tests are found in results.
    """
    markers = _build_generated_markers(added_tests)
    ignored_set = frozenset(ignored)

    generated: dict[str, str] = {}
    existing: dict[str, str] = {}

    for name, status in test_results.items():
        if not name.strip():
            continue
        if name in ignored_set:
            continue
        if _is_generated_test(name, markers):
            generated[name] = status
        else:
            existing[name] = status

    # Must have at least one generated test in the results
    if not generated:
        return False

    # All generated tests must PASS
    if not all(status == "PASSED" for status in generated.values()):
        return False

    # No P2F regressions in existing tests
    if any(status not in _PASS_STATUSES for status in existing.values()):
        return False

    return True


def build_combined_test_patch(
    added_tests: list[TestFile],
    deleted_tests: list[ExistingTestDeletion] | None = None,
) -> str:
    """Concatenate all added-test patches (and optional deletions) into a single patch string."""
    patches: list[str] = []
    for test_file in added_tests:
        patches.append(test_file.patch)
    if deleted_tests:
        for deletion in deleted_tests:
            patches.append(deletion.patch)
    return "\n".join(patches)
