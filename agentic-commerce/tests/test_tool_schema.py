"""A provider changing its tool schema must not kill the whole agent.

Swiggy added create_address and delete_address to a running server. The session
had cached its Gemini tool declarations at connect time, on the assumption that
"the declarations never change within a connection". They changed. Gemini then
answered every single turn with

    400 INVALID_ARGUMENT ... function_declarations[2]
    .parameters.properties[lines].items: missing field

and because that error is not "not connected", the bot fell through to its own
model, which had no share of the conversation and eventually invented a cart
priced in US dollars. One upstream schema edit, days of a confidently wrong
shopping assistant.

Two defences, tested here: convert schemas Gemini will actually accept, and
rebuild the cache instead of failing forever.
"""

import pytest

from backend.agent.loop import sanitize_json_schema
from backend.app import _looks_like_stale_tools


# ── the schema Gemini rejects ────────────────────────────────────────

def test_a_bare_array_gets_items():
    """`{"type": "array"}` is valid JSON Schema and means "array of anything".
    Gemini refuses the entire request over it."""
    out = sanitize_json_schema({"type": "array"})
    assert out["items"] == {"type": "string"}


def test_the_exact_shape_from_the_outage():
    schema = {
        "type": "object",
        "properties": {"lines": {"type": "array"}, "id": {"type": "string"}},
    }
    out = sanitize_json_schema(schema)
    assert "items" in out["properties"]["lines"]
    assert out["properties"]["id"] == {"type": "string"}


def test_it_recurses_into_nested_objects_and_arrays():
    schema = {
        "type": "object",
        "properties": {
            "outer": {
                "type": "object",
                "properties": {"inner": {"type": "array"}},
            },
            "listOfLists": {"type": "array", "items": {"type": "array"}},
        },
    }
    out = sanitize_json_schema(schema)
    assert "items" in out["properties"]["outer"]["properties"]["inner"]
    assert "items" in out["properties"]["listOfLists"]["items"]


def test_an_existing_items_is_never_overwritten():
    """Only the missing case is filled in; a declared element type is the
    provider's statement about its own API and must survive."""
    schema = {"type": "array", "items": {"type": "number", "description": "qty"}}
    assert sanitize_json_schema(schema)["items"] == {"type": "number", "description": "qty"}


def test_union_types_containing_array_are_handled():
    out = sanitize_json_schema({"type": ["array", "null"]})
    assert "items" in out


def test_the_providers_schema_is_not_mutated():
    """The adapter hands out a reference to its own cached schema."""
    original = {"type": "object", "properties": {"lines": {"type": "array"}}}
    snapshot = {"type": "object", "properties": {"lines": {"type": "array"}}}
    sanitize_json_schema(original)
    assert original == snapshot


@pytest.mark.parametrize("junk", [None, 3, "text", [], {}])
def test_non_schema_input_is_returned_unharmed(junk):
    assert sanitize_json_schema(junk) == junk


def test_real_swiggy_schemas_survive_a_round_trip():
    """The shapes actually published by Instamart and Food."""
    real = {
        "type": "object",
        "properties": {
            "addressId": {"type": "string", "description": "REQUIRED"},
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"spinId": {"type": "string"},
                                   "quantity": {"type": "number"}},
                },
            },
        },
        "required": ["addressId"],
    }
    out = sanitize_json_schema(real)
    assert out == real, "a well-formed schema must pass through untouched"


# ── recognising a stale cache ────────────────────────────────────────

def test_the_outage_error_is_recognised_as_stale_tools():
    exc = Exception(
        "400 INVALID_ARGUMENT. {'error': {'code': 400, 'message': "
        "'* GenerateContentRequest.tools[0].function_declarations[2]"
        ".parameters.properties[lines].items: missing field.\\n'}}"
    )
    assert _looks_like_stale_tools(exc) is True


@pytest.mark.parametrize("msg", [
    "Connection reset by peer",
    "429 RESOURCE_EXHAUSTED quota exceeded",
    "cart is empty",
])
def test_ordinary_failures_do_not_trigger_a_rebuild(msg):
    """Rebuilding costs a tools/list round trip. Doing it on every error would
    turn a rate limit into two rate limits."""
    assert _looks_like_stale_tools(Exception(msg)) is False
