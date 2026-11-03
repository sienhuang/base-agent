"""Exact SDK/runtime wheel compatibility checks."""

from __future__ import annotations

import importlib.metadata

PINNED_DSH_VERSION = "0.1.2a3"


class DshVersionError(RuntimeError):
    pass


def verify_dsh_versions(expected_version: str = PINNED_DSH_VERSION) -> tuple[str, str]:
    try:
        sdk_version = importlib.metadata.version("deepseek-harness-sdk")
        runtime_version = importlib.metadata.version("deepseek-harness-runtime-bin")
    except importlib.metadata.PackageNotFoundError as exc:
        raise DshVersionError(
            "deepseek-harness-sdk and its same-version runtime wheel are required"
        ) from exc
    if sdk_version != runtime_version:
        raise DshVersionError(
            f"DSH SDK/runtime mismatch: sdk={sdk_version}, runtime={runtime_version}"
        )
    if sdk_version != expected_version:
        raise DshVersionError(
            f"DSH version mismatch: expected={expected_version}, installed={sdk_version}"
        )
    return sdk_version, runtime_version
