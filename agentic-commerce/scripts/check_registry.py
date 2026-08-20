"""Connect to every enabled provider and print a capability matrix + drift report.

    python -m scripts.check_registry

The mock provider runs fully offline. The Swiggy provider WILL connect to
mcp.swiggy.com (and trigger the OAuth browser flow if no fresh token is cached),
so this is the one entry point in the foundations build that touches the network.
It performs read-only introspection (tools/list) — it never searches, carts, or
checks out.
"""

from __future__ import annotations

import anyio
from dotenv import load_dotenv

from backend.providers import registry


def _flags_str(flags) -> str:
    cap = flags.max_order_value
    cap_s = "—" if cap is None else f"₹{cap / 100:.0f}"
    return (
        f"anon_search={flags.searchable_without_auth!s:<5} "
        f"cart={flags.supports_cart!s:<5} checkout={flags.supports_checkout!s:<5} "
        f"pre_confirm={flags.requires_pre_confirmation!s:<5} cap={cap_s}"
    )


async def main():
    entries = registry.load_registry()
    adapters = registry.build_all(entries)

    print("\n=== Provider capability matrix (ADR-3) ===\n")
    for e in entries:
        status = "enabled" if e.enabled else "disabled"
        print(f"• {e.id:8} [{status}] {e.display_name}")
        print(f"    {_flags_str(e.declared_flags)}")
        if not e.enabled:
            continue
        report = await registry.check_provider(e, adapters[e.id])
        if report.error:
            print(f"    tools/list: ERROR — {report.error}")
        else:
            print(f"    tools/list: {len(report.actual_tools)} tools; "
                  f"missing_expected={report.missing_expected or 'none'}; "
                  f"extra={report.unexpected_extra or 'none'}")
        if report.flag_mismatches:
            print(f"    ⚠ FLAG DRIFT: {report.flag_mismatches}")
        print(f"    drift-check OK: {report.ok}\n")


if __name__ == "__main__":
    load_dotenv()
    anyio.run(main)
