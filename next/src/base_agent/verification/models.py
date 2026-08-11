"""Bounded evidence produced by output and stage verification."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class VerificationEvidence:
    verifier: str
    scope: str
    status: str
    detail: str = ""
