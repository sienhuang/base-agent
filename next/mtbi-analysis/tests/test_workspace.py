from pathlib import Path

import pytest

from mtbi_analysis.workspace import (
    ArtifactLimitError,
    ArtifactWorkspace,
    WorkspacePathError,
)


def _workspace(tmp_path: Path, *, max_rows: int = 10) -> ArtifactWorkspace:
    return ArtifactWorkspace(
        tmp_path,
        max_artifact_bytes=1024,
        max_csv_rows=max_rows,
    )


def test_workspace_rejects_absolute_and_escaping_paths(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)

    with pytest.raises(WorkspacePathError, match="relative"):
        workspace.write_text("/tmp/outside.txt", "unsafe")
    with pytest.raises(WorkspacePathError, match="escapes"):
        workspace.write_text("../outside.txt", "unsafe")


def test_csv_summary_is_bounded_and_contains_host_facts(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path, max_rows=2)
    workspace.write_text("data.csv", "name,value\na,2\nb,4\n")

    summary = workspace.summarize_csv("data.csv")

    assert summary["row_count"] == 2
    assert summary["columns"] == ("name", "value")
    assert summary["numeric_summary"]["value"] == {
        "count": 2,
        "min": 2.0,
        "max": 4.0,
        "mean": 3.0,
    }

    workspace.write_text("too-many.csv", "name\na\nb\nc\n")
    with pytest.raises(ArtifactLimitError, match="row limit"):
        workspace.summarize_csv("too-many.csv")


def test_csv_summary_rejects_header_only_files(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    workspace.write_text("empty.csv", "name,value\n")

    with pytest.raises(ValueError, match="data row"):
        workspace.summarize_csv("empty.csv")
