"""Exact CAF workspace-ID mapping for opt-in host fixtures."""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path


SAMPLE_BASE_REVISION = "e12f4d8293ea0dd8a5e18c3006af803c4d429b56"
_SAMPLE_IDS = frozenset({"caf-sample-fixture-v1", "caf-synthetic-v1"})


@dataclass(frozen=True, slots=True)
class WorkspaceBinding:
    workspace_id: str
    repository_slug: str
    repository: Path
    base_revision: str
    worktree_root: Path | None


def resolve_sample_workspace(umbrella_root: Path, workspace_id: str) -> WorkspaceBinding:
    """Map a known ID to the checked-out CAF submodule, never a task path."""
    if workspace_id not in _SAMPLE_IDS:
        raise ValueError("unknown CAF workspace ID")
    if not umbrella_root.is_absolute() or umbrella_root.is_symlink():
        raise ValueError("CAF umbrella root must be a real absolute directory")
    root = umbrella_root.resolve(strict=True)
    if not root.is_dir() or not (root / "SPEC.md").is_file():
        raise ValueError("CAF umbrella checkout is unavailable")
    expected = root / "repositories" / "caf-sample-app"
    if not expected.is_dir() or expected.is_symlink() or expected.resolve(strict=True) != expected:
        raise ValueError("CAF sample checkout is missing or redirected")

    worktree_root = None
    if workspace_id == "caf-sample-fixture-v1":
        temp_root = Path(tempfile.gettempdir())
        if temp_root.is_symlink() or temp_root.is_junction():
            raise ValueError("CAF temporary directory is redirected")
        worktree_root = temp_root.resolve(strict=True)
        if not worktree_root.is_dir() or worktree_root.is_relative_to(root):
            raise ValueError("CAF worktree root is unsafe")
    return WorkspaceBinding(workspace_id, "caf-sample-app", expected,
                            SAMPLE_BASE_REVISION, worktree_root)
