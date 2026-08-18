"""Deterministic verification for model-declared MTBI artifacts."""

from __future__ import annotations

import hashlib

from base_agent import Artifact, RunResult, VerificationEvidence

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
    sql_artifact = _required_artifact(
        run,
        name="query_sql",
        location=expected_sql_path,
        media_type="application/sql",
    )
    if sql_artifact.sha256 != hashlib.sha256(sql).hexdigest():
        raise ValueError("SQL workspace bytes do not match the committed Artifact")

    try:
        csv_summary = workspace.summarize_csv(expected_csv_path)
    except FileNotFoundError as exc:
        raise FileNotFoundError(
            _missing_artifact_detail(run, "CSV artifact was not created")
        ) from exc
    csv_artifact = _required_artifact(
        run,
        name="query_dataset",
        location=expected_csv_path,
        media_type="text/csv",
    )
    if csv_artifact.sha256 != csv_summary["sha256"]:
        raise ValueError("CSV workspace bytes do not match the committed Artifact")
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

    report_path = workspace.resolve(expected_report_path)
    report = workspace.read_bytes(expected_report_path)
    if not report.strip():
        raise ValueError("analysis report must not be empty")
    try:
        report.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("analysis report must be UTF-8") from exc
    report_artifact = _required_artifact(
        run,
        name="analysis_report",
        location=expected_report_path,
        media_type="text/markdown",
    )
    if report_artifact.sha256 != hashlib.sha256(report).hexdigest():
        raise ValueError("report workspace bytes do not match the committed Artifact")
    source = next(
        (
            artifact
            for artifact in run.artifact_inputs
            if artifact.name == "query_dataset"
        ),
        None,
    )
    if source is None:
        raise ValueError("analysis Run is missing its granted query_dataset Artifact")
    if source.location != expected_csv_path:
        raise ValueError("granted query_dataset location does not match the expected CSV")
    source_bytes = workspace.read_bytes(expected_csv_path)
    if source.sha256 != hashlib.sha256(source_bytes).hexdigest():
        raise ValueError("source CSV no longer matches the granted Artifact")
    if report_artifact.source_artifact_ids != (source.id,):
        raise ValueError("report Artifact provenance does not reference its granted dataset")
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


def _required_artifact(
    run: RunResult,
    *,
    name: str,
    location: str,
    media_type: str,
) -> Artifact:
    matches = [artifact for artifact in run.artifacts if artifact.name == name]
    if not matches:
        raise ValueError(f"Run did not commit required Artifact '{name}'")
    artifact = matches[-1]
    if artifact.location != location:
        raise ValueError(
            f"Artifact '{name}' location is '{artifact.location}', expected '{location}'"
        )
    if artifact.media_type != media_type:
        raise ValueError(
            f"Artifact '{name}' media type is '{artifact.media_type}', expected '{media_type}'"
        )
    return artifact
