"""Comparison orchestrator (ADR-4).

A separate, deterministic (non-LLM) service — NOT something the chat agent
decides to do (ADR-6). Given a canonical search query it:

1. Filters the registry to providers that are enabled AND either already
   connected for this user OR `searchable_without_auth = true`.
2. Calls each included provider's `search()` concurrently, each with a bounded
   per-provider timeout.
3. Returns as soon as all have responded or timed out, tagging every row
   `ok` / `timed_out` / `error` / `needs_connection` so the UI can explain WHY a
   provider is or isn't shown (FR2) rather than silently omitting it.

Wall-clock is the slowest single provider, not the sum — a slow provider can't
hang the whole comparison.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import anyio

from backend.canonical.types import (
    CanonicalProduct,
    CanonicalSearchQuery,
    CanonicalSearchResult,
    SearchStatus,
)
from backend.providers.base import ProviderAdapter

DEFAULT_TIMEOUT_S = 8.0


@dataclass
class ComparisonRow:
    provider_id: str
    display_name: str
    status: SearchStatus
    items: list[CanonicalProduct]
    error: Optional[str] = None


@dataclass
class ComparisonResult:
    query: str
    rows: list[ComparisonRow]

    @property
    def shown(self) -> list[ComparisonRow]:
        return [r for r in self.rows if r.status == SearchStatus.OK]

    def cheapest_by_provider(self) -> dict[str, Optional[int]]:
        """Lowest in-stock price (paise) each shown provider offers for the query —
        the kind of comparable number the comparison table renders."""
        out: dict[str, Optional[int]] = {}
        for r in self.shown:
            prices = [
                p.price.minor_units for p in r.items
                if p.available and p.price.minor_units is not None
            ]
            out[r.provider_id] = min(prices) if prices else None
        return out


@dataclass
class _Target:
    provider_id: str
    display_name: str
    adapter: ProviderAdapter
    connected: bool


async def compare(
    query: CanonicalSearchQuery,
    targets: list[_Target],
    *,
    per_provider_timeout_s: float = DEFAULT_TIMEOUT_S,
) -> ComparisonResult:
    rows: list[Optional[ComparisonRow]] = [None] * len(targets)

    async def _one(i: int, t: _Target) -> None:
        # Filter rule (ADR-4/ADR-5): auth-required providers the user hasn't
        # connected are reported as needs_connection, never silently dropped.
        if not (t.adapter.capability_flags.searchable_without_auth or t.connected):
            rows[i] = ComparisonRow(t.provider_id, t.display_name, SearchStatus.NEEDS_CONNECTION, [])
            return

        with anyio.move_on_after(per_provider_timeout_s) as scope:
            try:
                if not t.connected:
                    await t.adapter.connect()
                res: CanonicalSearchResult = await t.adapter.search(query)
                rows[i] = ComparisonRow(
                    t.provider_id, t.display_name, res.status, res.items, res.error
                )
                return
            except Exception as e:
                rows[i] = ComparisonRow(
                    t.provider_id, t.display_name, SearchStatus.ERROR, [], f"{type(e).__name__}: {e}"
                )
                return
        if scope.cancelled_caught:
            rows[i] = ComparisonRow(
                t.provider_id, t.display_name, SearchStatus.TIMED_OUT, [],
                f"exceeded {per_provider_timeout_s:.0f}s",
            )

    async with anyio.create_task_group() as tg:
        for i, t in enumerate(targets):
            tg.start_soon(_one, i, t)

    return ComparisonResult(query=query.text, rows=[r for r in rows if r is not None])


def build_targets(entries, adapters: dict, connected_ids: set[str]) -> list[_Target]:
    """Build the enabled-provider target list from a registry + connection state."""
    targets = []
    for e in entries:
        if not e.enabled or e.id not in adapters:
            continue
        targets.append(_Target(
            provider_id=e.id,
            display_name=e.display_name,
            adapter=adapters[e.id],
            connected=e.id in connected_ids,
        ))
    return targets
