"""Filesystem boundary for application-owned artifacts."""

from __future__ import annotations

import csv
import hashlib
import io
import os
import tempfile
from pathlib import Path
from typing import Any


class WorkspacePathError(ValueError):
    pass


class ArtifactLimitError(ValueError):
    pass


class ArtifactWorkspace:
    def __init__(
        self,
        root: Path,
        *,
        max_artifact_bytes: int,
        max_csv_rows: int,
    ) -> None:
        if max_artifact_bytes < 1:
            raise ValueError("max_artifact_bytes must be positive")
        if max_csv_rows < 1:
            raise ValueError("max_csv_rows must be positive")
        self.root = root.expanduser().resolve()
        self.max_artifact_bytes = max_artifact_bytes
        self.max_csv_rows = max_csv_rows
        self.root.mkdir(parents=True, exist_ok=True)

    def create_run_directory(self, run_token: str) -> Path:
        if not run_token or not run_token.replace("-", "").isalnum():
            raise ValueError("run_token must be alphanumeric with optional hyphens")
        path = self.resolve(f"runs/{run_token}")
        path.mkdir(parents=True, exist_ok=False)
        return path

    def resolve(self, relative_path: str) -> Path:
        if not relative_path.strip():
            raise WorkspacePathError("artifact path must not be blank")
        candidate = Path(relative_path)
        if candidate.is_absolute():
            raise WorkspacePathError("artifact path must be relative to the workspace")
        resolved = (self.root / candidate).resolve()
        try:
            resolved.relative_to(self.root)
        except ValueError as exc:
            raise WorkspacePathError("artifact path escapes the workspace") from exc
        return resolved

    def relative(self, path: Path) -> str:
        resolved = path.expanduser().resolve()
        try:
            return resolved.relative_to(self.root).as_posix()
        except ValueError as exc:
            raise WorkspacePathError("artifact path escapes the workspace") from exc

    def write_text(self, relative_path: str, content: str) -> dict[str, Any]:
        encoded = content.encode("utf-8")
        if len(encoded) > self.max_artifact_bytes:
            raise ArtifactLimitError(
                f"artifact exceeds {self.max_artifact_bytes} byte limit"
            )
        path = self.resolve(relative_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as temporary:
            temporary.write(encoded)
            temporary_path = Path(temporary.name)
        os.replace(temporary_path, path)
        return {
            "path": self.relative(path),
            "bytes": len(encoded),
            "sha256": hashlib.sha256(encoded).hexdigest(),
        }

    def read_bytes(self, relative_path: str) -> bytes:
        path = self.resolve(relative_path)
        size = path.stat().st_size
        if size > self.max_artifact_bytes:
            raise ArtifactLimitError(
                f"artifact exceeds {self.max_artifact_bytes} byte limit"
            )
        return path.read_bytes()

    def summarize_csv(self, relative_path: str) -> dict[str, Any]:
        raw = self.read_bytes(relative_path)
        text = raw.decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(text, newline=""))
        columns = tuple(reader.fieldnames or ())
        if not columns:
            raise ValueError("CSV is missing a header row")

        sample_rows: list[dict[str, str | None]] = []
        numeric_values: dict[str, list[float]] = {
            column: [0.0, 0.0, float("inf"), float("-inf")]
            for column in columns
        }
        row_count = 0
        for row in reader:
            row_count += 1
            if row_count > self.max_csv_rows:
                raise ArtifactLimitError(
                    f"CSV exceeds {self.max_csv_rows} row limit"
                )
            if len(sample_rows) < 20:
                sample_rows.append(dict(row))
            for column in columns:
                value = row.get(column)
                if value is None or not value.strip():
                    continue
                try:
                    number = float(value)
                except ValueError:
                    continue
                values = numeric_values[column]
                values[0] += 1
                values[1] += number
                values[2] = min(values[2], number)
                values[3] = max(values[3], number)
        if row_count == 0:
            raise ValueError("CSV must contain at least one data row")

        numeric_summary = {
            column: {
                "count": int(values[0]),
                "min": values[2],
                "max": values[3],
                "mean": values[1] / values[0],
            }
            for column, values in numeric_values.items()
            if values[0]
        }
        return {
            "path": self.relative(self.resolve(relative_path)),
            "bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "row_count": row_count,
            "columns": columns,
            "sample_rows": sample_rows,
            "numeric_summary": numeric_summary,
        }
