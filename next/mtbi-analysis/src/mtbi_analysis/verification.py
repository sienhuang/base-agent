"""Deterministic verification for model-declared MTBI artifacts."""

from __future__ import annotations

import hashlib

from base_agent import RunResult, VerificationEvidence

from mtbi_analysis.models import (
    MTBIAnalysisOutcome,
    MTBIQueryOutcome,
    VerifiedDataset,
    VerifiedReport,
)
from mtbi_analysis.workspace import ArtifactWorkspace


def verify_query_run(
    run: RunResult,
    *,
    workspace: ArtifactWorkspace,
    expected_sql_path: str,
    expected_csv_path: str,
) -> VerificationEvidence:
    try:
        validate_query_output(
            run,
            workspace=workspace,
            expected_sql_path=expected_sql_path,
            expected_csv_path=expected_csv_path,
        )
    except Exception as exc:
        return VerificationEvidence(
            verifier="mtbi_query_artifacts",
            scope="stage_output",
            status="failed",
            detail=f"{type(exc).__name__}: {exc}",
        )
    return VerificationEvidence(
        verifier="mtbi_query_artifacts",
        scope="stage_output",
        status="passed",
    )


def validate_query_output(
    run: RunResult,
    *,
    workspace: ArtifactWorkspace,
    expected_sql_path: str,
    expected_csv_path: str,
) -> VerifiedDataset:
    outcome = run.output
    if not isinstance(outcome, MTBIQueryOutcome):
        raise TypeError("query Agent did not return MTBIQueryOutcome")
    if not outcome.success:
        raise ValueError(f"query Agent reported failure: {outcome.error}")
    if outcome.sql_path != expected_sql_path:
        raise ValueError("query Agent returned an unexpected SQL path")
    if outcome.csv_path != expected_csv_path:
        raise ValueError("query Agent returned an unexpected CSV path")

    sql_path = workspace.resolve(expected_sql_path)
    csv_path = workspace.resolve(expected_csv_path)
    try:
        sql = workspace.read_bytes(expected_sql_path)
    except FileNotFoundError as exc:
        raise FileNotFoundError(
            _missing_artifact_detail(run, "SQL artifact was not created")
        ) from exc
    if not sql.strip():
        raise ValueError("SQL artifact must not be empty")
    try:
        sql_text = sql.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("SQL artifact must be UTF-8") from exc
    if "select" not in sql_text.lower():
        raise ValueError("SQL artifact must contain a SELECT query")

    try:
        csv_summary = workspace.summarize_csv(expected_csv_path)
    except FileNotFoundError as exc:
        raise FileNotFoundError(
            _missing_artifact_detail(run, "CSV artifact was not created")
        ) from exc
    return VerifiedDataset(
        sql_path=sql_path,
        csv_path=csv_path,
        sql_sha256=hashlib.sha256(sql).hexdigest(),
        csv_sha256=str(csv_summary["sha256"]),
        csv_bytes=int(csv_summary["bytes"]),
        row_count=int(csv_summary["row_count"]),
        columns=tuple(str(column) for column in csv_summary["columns"]),
    )


def verify_analysis_run(
    run: RunResult,
    *,
    workspace: ArtifactWorkspace,
    expected_csv_path: str,
    expected_report_path: str,
) -> VerificationEvidence:
    try:
        validate_analysis_output(
            run,
            workspace=workspace,
            expected_csv_path=expected_csv_path,
            expected_report_path=expected_report_path,
        )
    except Exception as exc:
        return VerificationEvidence(
            verifier="mtbi_analysis_report",
            scope="stage_output",
            status="failed",
            detail=f"{type(exc).__name__}: {exc}",
        )
    return VerificationEvidence(
        verifier="mtbi_analysis_report",
        scope="stage_output",
        status="passed",
    )


def validate_analysis_output(
    run: RunResult,
    *,
    workspace: ArtifactWorkspace,
    expected_csv_path: str,
    expected_report_path: str,
) -> VerifiedReport:
    outcome = run.output
    if not isinstance(outcome, MTBIAnalysisOutcome):
        raise TypeError("analysis Agent did not return MTBIAnalysisOutcome")
    if outcome.source_csv_path != expected_csv_path:
        raise ValueError("analysis Agent returned an unexpected source CSV path")
    if outcome.report_path != expected_report_path:
        raise ValueError("analysis Agent returned an unexpected report path")

    report_path = workspace.resolve(expected_report_path)
    report = workspace.read_bytes(expected_report_path)
    if not report.strip():
        raise ValueError("analysis report must not be empty")
    try:
        report.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("analysis report must be UTF-8") from exc
    return VerifiedReport(
        report_path=report_path,
        report_sha256=hashlib.sha256(report).hexdigest(),
        report_bytes=len(report),
    )


def _missing_artifact_detail(run: RunResult, message: str) -> str:
    failures = [
        f"{result.call.name}: {result.error}"
        for result in run.tool_results
        if result.error is not None
    ]
    if not failures:
        return message
    return f"{message}; last Tool failure: {failures[-1]}"
