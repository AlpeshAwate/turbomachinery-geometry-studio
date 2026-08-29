"""Deterministic engineering record for a pump design.

The record is intentionally independent from CAD objects.  It preserves the
inputs, selected correlations, derived values, validation evidence, and source
references that produced a geometry package.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from typing import Any, Mapping, Sequence


ENGINEERING_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class EngineeringCheck:
    """One deterministic engineering gate or advisory check."""

    key: str
    category: str
    status: str
    value: float | int | str
    unit: str
    source: str
    message: str
    lower_limit: float | None = None
    upper_limit: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class EngineeringRecord:
    """Versioned, serializable design evidence with a deterministic identity."""

    schema_version: int
    design_id: str
    source_documents: tuple[str, ...]
    correlations: Mapping[str, str]
    parameters: Mapping[str, Any]
    checks: tuple[EngineeringCheck, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "design_id": self.design_id,
            "source_documents": list(self.source_documents),
            "correlations": dict(self.correlations),
            "parameters": dict(self.parameters),
            "checks": [check.to_dict() for check in self.checks],
        }


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def create_engineering_record(
    *,
    parameters: Mapping[str, Any],
    correlations: Mapping[str, str],
    checks: Sequence[EngineeringCheck],
    source_documents: Sequence[str] = (
        "CFturbo_en.pdf",
        "docs/CFTURBO_GEOMETRY_REFERENCE.md",
    ),
) -> EngineeringRecord:
    """Create a stable record; equal engineering content receives the same ID."""

    identity_payload = {
        "schema_version": ENGINEERING_SCHEMA_VERSION,
        "source_documents": list(source_documents),
        "correlations": dict(correlations),
        "parameters": dict(parameters),
        "checks": [check.to_dict() for check in checks],
    }
    digest = hashlib.sha256(_canonical_json(identity_payload).encode("utf-8")).hexdigest()
    return EngineeringRecord(
        schema_version=ENGINEERING_SCHEMA_VERSION,
        design_id=f"pump-{digest[:16]}",
        source_documents=tuple(source_documents),
        correlations=dict(correlations),
        parameters=dict(parameters),
        checks=tuple(checks),
    )

