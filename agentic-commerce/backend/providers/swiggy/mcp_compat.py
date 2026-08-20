"""mcp-SDK version shims + the guarded Swiggy session.

Ported VERBATIM (behavior unchanged) from `swiggy_agent_gemini.py`. The mcp SDK
changed both its Streamable HTTP client and `CallToolResult`'s field names
between major versions; this module detects which is installed and adapts rather
than hardcoding one shape. `requirements.txt` pins `mcp==2.0.0` so in practice
the v2 path runs — but the legacy path is kept so a future bump degrades
gracefully instead of crashing.

The money-path guard (`GuardedSwiggySession`) is retained from the script: it
refuses to actually execute `checkout` / `confirm_order` unless armed for that
call. In this platform the *arming* decision is made upstream by the adapter/
agent-loop against provider-declared `CapabilityFlags` (ADR-6); this class stays
as the last-line, in-transport enforcement so a confused caller still can't spend
money without arming.
"""

from __future__ import annotations

import contextlib

from mcp import ClientSession
from mcp.types import CallToolResult, TextContent

from backend.providers.swiggy.auth import SWIGGY_MCP_URL

# The mcp SDK changed its Streamable HTTP client between major versions:
#   mcp <2.0:  streamablehttp_client(url, headers={...})
#   mcp >=2.0: streamable_http_client(url, http_client=<httpx2.AsyncClient>)
try:
    from mcp.client.streamable_http import streamablehttp_client as _raw_mcp_http_client
    _MCP_CLIENT_GEN = "legacy"
except ImportError:  # pragma: no cover - depends on installed mcp version
    from mcp.client.streamable_http import streamable_http_client as _raw_mcp_http_client
    _MCP_CLIENT_GEN = "v2"
    import httpx2


@contextlib.asynccontextmanager
async def open_swiggy_stream(access_token: str):
    """Yields (read_stream, write_stream) for the Swiggy MCP endpoint,
    authenticated with the given bearer token, regardless of which mcp SDK
    generation is installed. The bearer token only ever goes to mcp.swiggy.com."""
    headers = {"Authorization": f"Bearer {access_token}"}

    if _MCP_CLIENT_GEN == "legacy":
        async with _raw_mcp_http_client(SWIGGY_MCP_URL, headers=headers) as streams:
            yield streams[0], streams[1]
    else:
        async with httpx2.AsyncClient(headers=headers) as http_client:
            async with _raw_mcp_http_client(SWIGGY_MCP_URL, http_client=http_client) as streams:
                yield streams[0], streams[1]


# --------------------------------------------------------------------------
# CallToolResult field-name compatibility
#   mcp <2.0:  isError, structuredContent
#   mcp >=2.0: is_error, structured_content
# --------------------------------------------------------------------------

_ERROR_FIELD = "is_error" if "is_error" in CallToolResult.model_fields else "isError"


def make_blocked_result(text: str) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=text)],
        **{_ERROR_FIELD: True},
    )


def result_is_error(result: CallToolResult) -> bool:
    return bool(getattr(result, "is_error", None) or getattr(result, "isError", None))


def result_structured_content(result: CallToolResult):
    return getattr(result, "structured_content", None) or getattr(result, "structuredContent", None)


def extract_tool_result_text(result: CallToolResult) -> str:
    import json

    texts = [block.text for block in (result.content or []) if hasattr(block, "text")]
    if texts:
        return "\n".join(texts)
    structured = result_structured_content(result)
    return json.dumps(structured) if structured else ""


# --------------------------------------------------------------------------
# Guarded MCP session -- blocks dangerous tools unless armed
# --------------------------------------------------------------------------

# Swiggy's money-path tool names, re-exported from the manifest so there is one
# source of truth per provider. Importing it here rather than redefining it
# keeps the transport guard and the agent loop guarding the same set.
from backend.providers.swiggy.manifest import DANGEROUS_TOOLS  # noqa: E402


class GuardedSwiggySession(ClientSession):
    """Same as ClientSession, but refuses to actually execute the dangerous
    tools unless `.armed` is True for this call. Enforced in Python (not just a
    prompt instruction). Arming is single-shot: it auto-resets after one call."""

    armed = False
    dangerous_tools = DANGEROUS_TOOLS

    async def call_tool(self, name, arguments=None, **kwargs):
        if name in self.dangerous_tools and not GuardedSwiggySession.armed:
            return make_blocked_result(
                f"BLOCKED: '{name}' requires explicit user confirmation before it can run."
            )
        try:
            return await super().call_tool(name, arguments, **kwargs)
        finally:
            GuardedSwiggySession.armed = False  # single-turn arm only
