"""Phase B (step 4) — comparison orchestrator + capability matrix. No network:
uses the in-memory mock plus tiny inline fakes to exercise every status tag.
"""

from __future__ import annotations

import anyio

from backend.canonical.types import (
    CanonicalProduct,
    CanonicalSearchQuery,
    CanonicalSearchResult,
    Money,
    SearchStatus,
)
from backend.comparison import matrix, orchestrator
from backend.comparison.orchestrator import _Target
from backend.providers import registry
from backend.providers._mock_provider.adapter import MockAdapter
from backend.providers.base import CapabilityFlags


class _Fake:
    """Minimal adapter for orchestrator tests."""

    def __init__(self, pid, *, anon, behavior="ok", price=5000):
        self.id = pid
        self.capability_flags = CapabilityFlags(searchable_without_auth=anon, supports_checkout=False)
        self._behavior = behavior
        self._price = price
        self.connected = False

    async def connect(self):
        self.connected = True

    async def close(self):
        self.connected = False

    async def search(self, query: CanonicalSearchQuery) -> CanonicalSearchResult:
        if self._behavior == "error":
            raise RuntimeError("boom")
        if self._behavior == "slow":
            await anyio.sleep(5)
        return CanonicalSearchResult(
            self.id, SearchStatus.OK,
            items=[CanonicalProduct(self.id, f"{self.id}-1", "Item", Money(self._price))],
        )


def _t(adapter, connected=False, name=None):
    return _Target(adapter.id, name or adapter.id, adapter, connected)


async def test_ok_and_needs_connection_and_error_and_timeout_tags():
    q = CanonicalSearchQuery(text="milk")
    targets = [
        _t(_Fake("anon_ok", anon=True, behavior="ok", price=4000)),
        _t(_Fake("auth_missing", anon=False), connected=False),         # -> needs_connection
        _t(_Fake("auth_connected", anon=False, price=3000), connected=True),  # -> ok
        _t(_Fake("boom", anon=True, behavior="error")),                 # -> error
        _t(_Fake("slow", anon=True, behavior="slow")),                  # -> timed_out
    ]
    res = await orchestrator.compare(q, targets, per_provider_timeout_s=0.3)
    by_id = {r.provider_id: r.status for r in res.rows}
    assert by_id["anon_ok"] == SearchStatus.OK
    assert by_id["auth_missing"] == SearchStatus.NEEDS_CONNECTION
    assert by_id["auth_connected"] == SearchStatus.OK
    assert by_id["boom"] == SearchStatus.ERROR
    assert by_id["slow"] == SearchStatus.TIMED_OUT


async def test_cheapest_by_provider():
    q = CanonicalSearchQuery(text="milk")
    targets = [
        _t(_Fake("a", anon=True, price=4000)),
        _t(_Fake("b", anon=True, price=2500)),
    ]
    res = await orchestrator.compare(q, targets)
    assert res.cheapest_by_provider() == {"a": 4000, "b": 2500}
    assert len(res.shown) == 2


async def test_slow_provider_does_not_block_fast_one():
    q = CanonicalSearchQuery(text="milk")
    targets = [_t(_Fake("fast", anon=True)), _t(_Fake("slow", anon=True, behavior="slow"))]
    with anyio.move_on_after(2):  # whole thing must finish well under slow's 5s
        res = await orchestrator.compare(q, targets, per_provider_timeout_s=0.3)
    fast = next(r for r in res.rows if r.provider_id == "fast")
    assert fast.status == SearchStatus.OK


async def test_mock_included_in_comparison_via_registry():
    entries = registry.load_registry("providers.yaml")
    adapters = registry.build_all(entries)
    # No providers "connected": only the anonymous-searchable mock should run;
    # Swiggy should come back needs_connection (ADR-5), not error or silent.
    targets = orchestrator.build_targets(entries, adapters, connected_ids=set())
    res = await orchestrator.compare(CanonicalSearchQuery(text="milk"), targets)
    by_id = {r.provider_id: r.status for r in res.rows}
    assert by_id["mock"] == SearchStatus.OK
    assert by_id["swiggy"] == SearchStatus.NEEDS_CONNECTION


# --------------------------------------------------------------------------
# Capability matrix (FR3)
# --------------------------------------------------------------------------

async def test_matrix_offline_has_declared_flags():
    entries = registry.load_registry("providers.yaml")
    adapters = registry.build_all(entries)
    rows = await matrix.build_matrix(entries, adapters, live=False)
    by_id = {r.provider_id: r for r in rows}
    assert by_id["swiggy"].flags["searchable_without_auth"] is False
    assert by_id["swiggy"].flags["max_order_value"] == 100000
    assert by_id["mock"].flags["searchable_without_auth"] is True
    # offline: no live drift fields populated
    assert by_id["mock"].tool_count is None
    # serializable for the HTTP layer
    assert isinstance(matrix.matrix_to_dict(rows), list)


async def test_matrix_live_mock_only_is_clean():
    entries = [e for e in registry.load_registry("providers.yaml") if e.id == "mock"]
    adapters = registry.build_all(entries)
    rows = await matrix.build_matrix(entries, adapters, live=True)
    mock = rows[0]
    assert mock.connected is True
    assert mock.tool_count == 6
    assert mock.drift_ok is True
