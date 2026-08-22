"""Swiggy Food parsers, against payloads captured from the live server.

Every fixture here is real JSON from mcp.swiggy.com/food on 2026-08-21, trimmed
but not reshaped. That matters: the previous tests for this provider asserted
tool names, endpoints, guards and capability flags, and all of them passed while
**both parsers raised TypeError on their first line** and every Food call failed
with "addressId is required". Nothing had ever run against a real response.

The three defects these lock down:

  * `addressId` is required on search, menu and cart. Swiggy's published
    reference does not mark it required; the live schema does.
  * `CanonicalProduct` takes `provider_id` first and `CanonicalCartItem` has no
    `unit_price` -- so construction failed before any field mapping mattered.
  * The casing is inconsistent *within* the service: camelCase from search,
    snake_case from the cart, both mixed in search_menu.
"""

import pytest

from backend.providers.swiggy import manifest_food as mf

# --- captured 2026-08-21, mcp.swiggy.com/food -------------------------------

SEARCH_RESTAURANTS = {
    "restaurants": [
        {
            "id": "875705",
            "name": "The Pizza Bakery - Wood Fired Sourdough",
            "cuisines": ["Pizzas", "Italian", "Pastas"],
            "avgRating": 4.4,
            "totalRatings": "3.6K+",
            "costForTwo": "₹1500 for two",
            "areaName": "HSR",
            "distanceKm": 1.1,
            "deliveryTimeMinutes": 20,
            "deliveryTimeRange": "15-20 MINS",
            "offer": "70% OFF UPTO ₹140",
            "imageUrl": "https://media-assets.swiggy.com/x",
            "availabilityStatus": "OPEN",
        },
        {"id": "999", "name": "Shut Kitchen", "cuisines": ["Thai"],
         "availabilityStatus": "CLOSED"},
    ],
    "total": 2, "hasMore": False, "nextOffset": 10,
}

SEARCH_MENU = {
    "items": [{
        "name": "Hyderabadi Chicken Dum Biryani",
        "price": 389,
        "menu_item_id": "65067727",
        "inStock": 1,
        "imageUrl": "https://media-assets.swiggy.com/y",
        "restaurant_id": "348362",
        "restaurant_name": "Andhra Gunpowder",
        "rating": "4.4",
        "hasAddons": True,
    }],
    "total": 1,
}

GET_FOOD_CART = {
    "statusCode": 0,
    "statusMessage": "CART_UPDATED_SUCCESSFULLY",
    "addressId": "da3ahcc1d96j569b7vg0",
    "data": {
        "cart_id": 730218441,
        "result": "success",
        "restaurant": {"deliverySubtitle": "20-25 mins to Office"},
        "items": [{
            "menu_item_id": 73030562,
            "name": "Matar Poori",
            "quantity": 1,
            "is_veg": "1",
            "subtotal": 349,
            "total": 359,
            "final_price": 349,
            "in_stock": 1,
        }],
        "item_count": 1,
        "pricing": {
            "item_total": 349,
            "delivery_charge": 40,
            "delivery_charge_strikeoff": 40,
            "taxes_and_charges": 52.73,
            "to_pay": 442,
        },
    },
}


# ── restaurants ──────────────────────────────────────────────────────

def test_restaurants_parse_at_all():
    """The first draft raised TypeError here: CanonicalProduct's first field is
    provider_id and it was never passed."""
    out = mf.parse_restaurants(SEARCH_RESTAURANTS)
    assert len(out) == 2
    assert all(p.provider_id == "swiggy_food" for p in out)


def test_the_id_field_is_id_not_restaurant_id():
    assert mf.parse_restaurants(SEARCH_RESTAURANTS)[0].product_id == "875705"


def test_a_closed_restaurant_is_not_reported_as_open():
    """availabilityStatus is the string "CLOSED", not a boolean.

    Reading it with bool(r.get("isOpen", True)) finds no such key and falls back
    to True -- failing in the direction that sends an order to a shut kitchen.
    """
    shut = [p for p in mf.parse_restaurants(SEARCH_RESTAURANTS) if p.name == "Shut Kitchen"]
    assert shut and shut[0].available is False


def test_cost_for_two_is_not_presented_as_a_price():
    """"₹1500 for two" is an estimate for two people, not a price anyone pays.

    Putting it in `price` makes the renderer announce "Cheapest: ₹1500" about a
    number that is not the cost of anything orderable.
    """
    top = mf.parse_restaurants(SEARCH_RESTAURANTS)[0]
    assert top.price.minor_units is None
    assert "1500" in (top.unit or ""), "the estimate should still be shown, as text"
    assert "4.4" in (top.unit or "") and "MINS" in (top.unit or "")


# ── dishes ───────────────────────────────────────────────────────────

def test_dishes_carry_a_real_price():
    d = mf.parse_dishes(SEARCH_MENU)[0]
    assert d.provider_id == "swiggy_food"
    assert d.product_id == "65067727"     # menu_item_id, snake_case
    assert d.price.minor_units == 38900   # 389 rupees -> paise
    assert d.available is True            # inStock: 1, camelCase, integer
    assert "Andhra Gunpowder" in (d.unit or "")


def test_an_out_of_stock_dish_is_marked_so():
    payload = {"items": [dict(SEARCH_MENU["items"][0], inStock=0)]}
    assert mf.parse_dishes(payload)[0].available is False


# ── cart ─────────────────────────────────────────────────────────────

def test_cart_parses_at_all():
    """The first draft passed unit_price= to CanonicalCartItem, which has no
    such field, and raw= to a dataclass that has no raw."""
    cart = mf.parse_cart(GET_FOOD_CART)
    assert len(cart.items) == 1


def test_the_total_comes_from_pricing_to_pay():
    """snake_case `to_pay`, nested under `data.pricing`.

    Looking for camelCase `toPay` at the top level found nothing and returned a
    total of None, which renders as an em dash.
    """
    assert mf.parse_cart(GET_FOOD_CART).total.minor_units == 44200


def test_subtotal_and_total_differ_because_fees_exist():
    cart = mf.parse_cart(GET_FOOD_CART)
    assert cart.subtotal.minor_units == 34900
    assert cart.total.minor_units > cart.subtotal.minor_units


def test_the_itemised_bill_survives_into_the_confirmation():
    """₹349 of food costs ₹442, and the person tapping Approve should see why."""
    lines = mf.parse_cart(GET_FOOD_CART).bill_breakdown["lineItems"]
    labels = {l["label"] for l in lines}
    assert {"Item Total", "Delivery", "Taxes and Charges"} <= labels
    taxes = next(l for l in lines if l["label"] == "Taxes and Charges")
    assert "52.73" in taxes["value"], "a float charge must not be truncated"


def test_cart_item_fields_are_snake_case_here():
    item = mf.parse_cart(GET_FOOD_CART).items[0]
    assert item.product_id == "73030562"   # menu_item_id
    assert item.name == "Matar Poori"
    assert item.price.minor_units == 34900  # final_price, per unit


def test_unit_price_uses_final_price_not_total():
    """`total` (359) includes addons; `final_price` (349) is the unit price.

    Using `total` as a unit price multiplies addons by the quantity a second
    time inside line_total_minor.
    """
    payload = {"data": dict(GET_FOOD_CART["data"],
                            items=[dict(GET_FOOD_CART["data"]["items"][0], quantity=3)])}
    item = mf.parse_cart(payload).items[0]
    assert item.price.minor_units == 34900
    assert item.line_total_minor == 104700


# ── the failure that started this ────────────────────────────────────

def test_the_address_scoped_tools_are_declared():
    """Food errors with a bare "addressId is required" on every one of these.

    The adapter injects it; this pins the list so a newly-mapped tool that needs
    one is not silently left out.
    """
    for tool in ("search_restaurants", "search_menu", "get_restaurant_menu",
                 "get_food_cart", "update_food_cart", "place_food_order"):
        assert tool in mf.ADDRESS_SCOPED


def test_every_mapped_tool_exists_on_the_live_server():
    """TOOL_MAP values must all be in the verified live tool set."""
    missing = sorted(set(mf.TOOL_MAP.values()) - mf.EXPECTED_TOOLS)
    assert not missing, f"TOOL_MAP names absent from the live server: {missing}"


def test_malformed_payloads_do_not_raise():
    """Tool results reach the model as text; a parser that raises kills the
    turn instead of letting it recover."""
    for junk in (None, [], "", {"text": "some prose"}, {"restaurants": [None, 3]},
                 {"data": {"items": [None]}}):
        mf.parse_restaurants(junk)
        mf.parse_dishes(junk)
        mf.parse_cart(junk)
