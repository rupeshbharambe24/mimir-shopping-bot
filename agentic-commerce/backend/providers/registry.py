"""Provider registry (ADR-3).

Loads `providers.yaml`, instantiates each provider's adapter from a config
pointer, and (on demand) runs a `tools/list` drift check: it connects to a
provider and compares what its MCP server ACTUALLY exposes against what the
manifest expected, and the registry-declared capability flags against what the
adapter code declares. Mismatches surface as data (for the FR3 matrix later),
not as silent failures.

Adding a provider = one YAML entry + its manifest/adapter module. No code here
changes.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from typing import Any, Optional

import yaml

from backend.providers.base import CapabilityFlags, ProviderAdapter


@dataclass
class ProviderEntry:
    id: str
    display_name: str
    enabled: bool
    mcp_url: Optional[str]
    auth_type: str
    adapter_path: str          # "module.path:ClassName"
    manifest_module: str       # "module.path" exposing EXPECTED_TOOLS
    declared_flags: CapabilityFlags


@dataclass
class DriftReport:
    provider_id: str
    connected: bool
    actual_tools: list[str] = field(default_factory=list)
    missing_expected: list[str] = field(default_factory=list)   # expected but absent on server
    unexpected_extra: list[str] = field(default_factory=list)   # on server but not expected
    flag_mismatches: list[str] = field(default_factory=list)    # yaml vs adapter code
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return (
            self.connected
            and not self.missing_expected
            and not self.flag_mismatches
            and self.error is None
        )


def load_registry(path: str = "providers.yaml") -> list[ProviderEntry]:
    with open(path) as f:
        doc = yaml.safe_load(f) or {}
    entries: list[ProviderEntry] = []
    for raw in doc.get("providers", []):
        flags_raw = raw.get("capability_flags", {}) or {}
        entries.append(ProviderEntry(
            id=raw["id"],
            display_name=raw.get("display_name", raw["id"]),
            enabled=bool(raw.get("enabled", True)),
            mcp_url=raw.get("mcp_url"),
            auth_type=raw.get("auth_type", "none"),
            adapter_path=raw["adapter"],
            manifest_module=raw["manifest"],
            declared_flags=CapabilityFlags(**flags_raw),
        ))
    return entries


def build_adapter(entry: ProviderEntry) -> ProviderAdapter:
    module_path, _, class_name = entry.adapter_path.partition(":")
    module = importlib.import_module(module_path)
    cls = getattr(module, class_name)
    return cls()


def _expected_tools(entry: ProviderEntry) -> set[str]:
    module = importlib.import_module(entry.manifest_module)
    return set(getattr(module, "EXPECTED_TOOLS", set()))


def _compare_flags(entry: ProviderEntry, adapter: ProviderAdapter) -> list[str]:
    mismatches = []
    declared = entry.declared_flags
    actual = adapter.capability_flags
    for fname in vars(declared):
        dv, av = getattr(declared, fname), getattr(actual, fname)
        if dv != av:
            mismatches.append(f"{fname}: yaml={dv!r} != code={av!r}")
    return mismatches


async def check_provider(entry: ProviderEntry, adapter: ProviderAdapter) -> DriftReport:
    """Connect, list tools, and diff against expectations. Never raises — a
    provider that can't be reached comes back as a report with `error` set."""
    report = DriftReport(provider_id=entry.id, connected=False)
    report.flag_mismatches = _compare_flags(entry, adapter)

    try:
        await adapter.connect()
        report.connected = True
        actual = {t.name for t in await adapter.list_raw_tools()}
        report.actual_tools = sorted(actual)
        expected = _expected_tools(entry)
        report.missing_expected = sorted(expected - actual)
        report.unexpected_extra = sorted(actual - expected)
    except Exception as e:  # unreachable / auth failure / etc.
        report.error = f"{type(e).__name__}: {e}"
    finally:
        with_suppressed_close = getattr(adapter, "close", None)
        if with_suppressed_close is not None:
            try:
                await adapter.close()
            except Exception:
                pass
    return report


def build_all(entries: list[ProviderEntry]) -> dict[str, ProviderAdapter]:
    """Instantiate adapters for all ENABLED providers, keyed by id."""
    return {e.id: build_adapter(e) for e in entries if e.enabled}
