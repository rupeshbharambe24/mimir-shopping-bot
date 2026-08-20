"""Capture Zepto's real tool surface after login.

Its tool names and JSON shapes are not documented anywhere and tools/list is
behind auth, so the manifest cannot be written until this has been run once.

Read-only: lists tools and calls only tools whose names look non-mutating.
Nothing here can place an order.
"""

import json
import sys

import anyio

sys.path.insert(0, ".")
from dotenv import load_dotenv  # noqa: E402

load_dotenv(".env")

from backend.providers.zepto import auth  # noqa: E402


async def main() -> None:
    import contextlib

    from mcp import ClientSession

    from backend.providers.swiggy.mcp_compat import _MCP_CLIENT_GEN, _raw_mcp_http_client

    token = auth.get_access_token()
    headers = {"Authorization": f"Bearer {token}"}

    async with contextlib.AsyncExitStack() as stack:
        if _MCP_CLIENT_GEN == "legacy":
            streams = await stack.enter_async_context(
                _raw_mcp_http_client(auth.ZEPTO_MCP_URL, headers=headers)
            )
        else:
            import httpx2
            client = await stack.enter_async_context(httpx2.AsyncClient(headers=headers))
            streams = await stack.enter_async_context(
                _raw_mcp_http_client(auth.ZEPTO_MCP_URL, http_client=client)
            )
        session = await stack.enter_async_context(ClientSession(streams[0], streams[1]))
        await session.initialize()

        listed = await session.list_tools()
        tools = listed.tools
        print(f"=== ZEPTO EXPOSES {len(tools)} TOOLS ===\n")
        dump = []
        for t in tools:
            schema = getattr(t, "inputSchema", None) or getattr(t, "input_schema", None) or {}
            props = list((schema or {}).get("properties", {}) or {})
            print(f"  {t.name}")
            print(f"      args: {props}")
            print(f"      {(t.description or '')[:220]}")
            print()
            dump.append({"name": t.name, "description": t.description, "input_schema": schema})

        payload = json.dumps(dump)
        print(f"Schema payload: {len(payload)} chars ~= {len(payload)//4} tokens per LLM call")

        with open("zepto_tools.json", "w") as f:
            json.dump(dump, f, indent=2)
        print("Full dump written to zepto_tools.json")

        # Flag anything that could place an order, so the money-path guard can
        # be configured with the right names rather than Swiggy's.
        RISKY = ("checkout", "order", "place", "pay", "confirm", "purchase", "buy")
        risky = [t.name for t in tools if any(r in t.name.lower() for r in RISKY)]
        print(f"\n!! Candidate money-path tools (guard must cover these): {risky}")


anyio.run(main)
