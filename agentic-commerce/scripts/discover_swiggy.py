"""Capture a Swiggy service's REAL tool surface and diff it against the manifest.

The Food and Dineout manifests were written from Swiggy's published reference,
not from a live server, and this codebase already knows what that is worth: the
Instamart docs listed 16 tools, the server exposed 14, three documented tools
were absent and one present tool was undocumented. Swiggy's own pages disagree
with each other on the Food count (14 vs 17).

So EXPECTED_TOOLS in manifest_food / manifest_dineout are hypotheses. This
script turns them into facts.

Read-only. It calls tools/list and nothing else -- no tool is invoked, so it
cannot place an order or book a table.

    python -m scripts.discover_swiggy food
    python -m scripts.discover_swiggy dineout
    python -m scripts.discover_swiggy im
"""

import contextlib
import json
import sys

import anyio

sys.path.insert(0, ".")
from dotenv import load_dotenv  # noqa: E402

load_dotenv(".env")

from backend.providers.swiggy import auth, manifest, manifest_dineout, manifest_food  # noqa: E402
from backend.providers.swiggy.mcp_compat import open_swiggy_stream  # noqa: E402

SERVICES = {
    "im": ("Instamart", auth.SWIGGY_MCP_URL, manifest),
    "food": ("Food", manifest_food.MCP_URL, manifest_food),
    "dineout": ("Dineout", manifest_dineout.MCP_URL, manifest_dineout),
}


async def main() -> None:
    from mcp import ClientSession

    which = (sys.argv[1] if len(sys.argv) > 1 else "food").strip().lstrip("/")
    if which not in SERVICES:
        print(f"Unknown service {which!r}. Pick one of: {', '.join(SERVICES)}")
        raise SystemExit(2)

    label, url, mod = SERVICES[which]
    token = auth.get_access_token()

    print(f"\n=== Swiggy {label} — {url} ===\n")

    async with contextlib.AsyncExitStack() as stack:
        read_stream, write_stream = await stack.enter_async_context(
            open_swiggy_stream(token, url)
        )
        # A plain ClientSession, not the guarded one: nothing is called here, and
        # using the guard would imply this script might spend something.
        session = await stack.enter_async_context(ClientSession(read_stream, write_stream))
        await session.initialize()
        listed = await session.list_tools()

    actual = {t.name for t in listed.tools}
    expected = set(getattr(mod, "EXPECTED_TOOLS", set()))
    dangerous = set(getattr(mod, "DANGEROUS_TOOLS", set()))

    for t in sorted(listed.tools, key=lambda x: x.name):
        flag = "  <-- MONEY PATH" if t.name in dangerous else ""
        print(f"  {t.name}{flag}")
        desc = (t.description or "").strip().splitlines()
        if desc:
            print(f"      {desc[0][:110]}")

    print(f"\n  {len(actual)} tools live, {len(expected)} expected by the manifest\n")

    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    if missing:
        print(f"  EXPECTED BUT ABSENT   {missing}")
    if extra:
        print(f"  PRESENT BUT UNLISTED  {extra}")
    if not missing and not extra:
        print("  Manifest matches the live server exactly.")

    # The check that actually matters.
    unguarded = sorted(
        n for n in actual
        if n not in dangerous
        and any(w in n.lower() for w in ("place", "book", "checkout", "confirm", "pay"))
        and not n.lower().startswith(("get_", "check_", "list_"))
    )
    if unguarded:
        print(
            "\n  ⚠️  POSSIBLE UNGUARDED MONEY TOOLS: "
            f"{unguarded}\n      Add them to {mod.__name__}.DANGEROUS_TOOLS or the "
            "guard will never fire for them."
        )

    print("\n  Paste the live set into EXPECTED_TOOLS and drop the # VERIFY note.\n")
    print(json.dumps(sorted(actual), indent=2))


if __name__ == "__main__":
    anyio.run(main)
