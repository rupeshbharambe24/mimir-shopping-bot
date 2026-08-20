"""Capability matrix builder (FR3).

Turns the registry (+ optional live `tools/list` drift check) into a structured
matrix the UI renders: one row per provider, its declared capability flags, and
— when run live — how its real tools compare to what the manifest expected.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Optional

from backend.providers import registry
from backend.providers.base import CapabilityFlags, ProviderAdapter
from backend.providers.registry import ProviderEntry


@dataclass
class MatrixRow:
    provider_id: str
    display_name: str
    enabled: bool
    auth_type: str
    flags: dict            # CapabilityFlags as a plain dict for the UI
    # live-only fields (None when built offline):
    connected: Optional[bool] = None
    tool_count: Optional[int] = None
    missing_expected: list[str] = field(default_factory=list)
    unexpected_extra: list[str] = field(default_factory=list)
    drift_ok: Optional[bool] = None
    error: Optional[str] = None


def _flags_dict(flags: CapabilityFlags) -> dict:
    return asdict(flags)


async def build_matrix(
    entries: list[ProviderEntry],
    adapters: dict[str, ProviderAdapter],
    *,
    live: bool = True,
) -> list[MatrixRow]:
    """Build the matrix. `live=False` uses only declared config (no network),
    which is what offline tests and quick UI loads use; `live=True` also runs the
    ADR-3 drift check per enabled provider."""
    rows: list[MatrixRow] = []
    for e in entries:
        row = MatrixRow(
            provider_id=e.id,
            display_name=e.display_name,
            enabled=e.enabled,
            auth_type=e.auth_type,
            flags=_flags_dict(e.declared_flags),
        )
        if live and e.enabled and e.id in adapters:
            report = await registry.check_provider(e, adapters[e.id])
            row.connected = report.connected
            row.tool_count = len(report.actual_tools) if report.connected else None
            row.missing_expected = report.missing_expected
            row.unexpected_extra = report.unexpected_extra
            row.drift_ok = report.ok
            row.error = report.error
        rows.append(row)
    return rows


def matrix_to_dict(rows: list[MatrixRow]) -> list[dict]:
    """JSON-serializable form for the HTTP/WS layer."""
    return [asdict(r) for r in rows]
