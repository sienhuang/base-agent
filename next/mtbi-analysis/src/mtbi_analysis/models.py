"""Typed application outputs and verified artifact facts."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _StrictOutcome(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )


class MTBIQueryOutcome(_StrictOutcome):
    success: bool
    sql_path: str | None
    csv_path: str | None
    selected_tables: tuple[str, ...]
    confirmed_columns: tuple[str, ...]
    summary: str = Field(min_length=1)
    error: str | None

    @model_validator(mode="after")
    def validate_result(self) -> MTBIQueryOutcome:
        if self.success:
            if not self.sql_path or not self.csv_path:
                raise ValueError("successful query requires SQL and CSV paths")
            if not self.selected_tables or not self.confirmed_columns:
                raise ValueError("successful query requires confirmed table and column metadata")
            if self.error is not None:
                raise ValueError("successful query must not include an error")
        elif not self.error:
            raise ValueError("unsuccessful query requires an error")
        return self


class MTBIAnalysisOutcome(_StrictOutcome):
    report_path: str = Field(min_length=1)
    source_csv_path: str = Field(min_length=1)
    summary: str = Field(min_length=1)


@dataclass(frozen=True, slots=True)
class VerifiedDataset:
    sql_path: Path
    csv_path: Path
    sql_sha256: str
    csv_sha256: str
    csv_bytes: int
    row_count: int
    columns: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class VerifiedReport:
    report_path: Path
    report_sha256: str
    report_bytes: int
