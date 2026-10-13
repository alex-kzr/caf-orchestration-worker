from pathlib import Path

import pytest

from caf_orchestration_worker.workspace_binding import (
    SAMPLE_BASE_REVISION, resolve_sample_workspace,
)
from caf_orchestration_worker import workspace_binding


def fake_umbrella(tmp_path: Path) -> Path:
    root = tmp_path / "caf"
    (root / "repositories" / "caf-sample-app").mkdir(parents=True)
    (root / "SPEC.md").write_text("test fixture\n", encoding="utf-8")
    return root


def test_exact_workspace_ids_resolve_only_sample_submodule(tmp_path):
    root = fake_umbrella(tmp_path)
    source = root / "repositories" / "caf-sample-app"
    synthetic = resolve_sample_workspace(root, "caf-synthetic-v1")
    assert synthetic.repository == source
    assert synthetic.repository_slug == "caf-sample-app"
    assert synthetic.base_revision == SAMPLE_BASE_REVISION
    assert synthetic.worktree_root is None
    code = resolve_sample_workspace(root, "caf-sample-fixture-v1")
    assert code.repository == source
    assert code.worktree_root is not None
    assert not code.worktree_root.is_relative_to(root)
    with pytest.raises(ValueError, match="unknown CAF workspace"):
        resolve_sample_workspace(root, "other-project")
    with pytest.raises(ValueError, match="real absolute"):
        resolve_sample_workspace(Path("caf"), "caf-synthetic-v1")


def test_redirected_checkout_is_rejected_before_claim(tmp_path):
    root = fake_umbrella(tmp_path)
    source = root / "repositories" / "caf-sample-app"
    source.rmdir()
    outside = tmp_path / "unrelated-project"
    outside.mkdir()
    try:
        source.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("host does not permit directory symlinks")
    with pytest.raises(ValueError, match="redirected"):
        resolve_sample_workspace(root, "caf-synthetic-v1")


def test_redirected_temp_root_is_rejected_before_claim(tmp_path, monkeypatch):
    root = fake_umbrella(tmp_path)
    temp_root = tmp_path / "other-temp"
    temp_root.mkdir()
    monkeypatch.setattr(workspace_binding.tempfile, "gettempdir", lambda: str(temp_root))
    original = Path.is_junction
    monkeypatch.setattr(Path, "is_junction", lambda self: self == temp_root or original(self))
    with pytest.raises(ValueError, match="redirected"):
        resolve_sample_workspace(root, "caf-sample-fixture-v1")
