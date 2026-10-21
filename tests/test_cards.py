import json

import pytest

from core import cards


def metadata(msg):
    meta = [c for c in msg.content if c.type == "metadata"]
    return meta[0].metadata if meta else None


def body(msg):
    return next(c.text for c in msg.content if c.type == "text")


def test_review_card_message():
    card = cards.review("Sale to Ramesh Kumar", [{"label": "Total", "value": "₹4,200"}], "req-1")
    msg = cards.card_message(card, "Check and confirm:")
    meta = metadata(msg)
    assert meta["card_protocol_version"] == "1"
    assert meta["requires_card_interaction"] == "true"
    assert meta["card_kind"] == "review"
    assert "is_terminal" not in meta
    assert all(isinstance(v, str) for v in meta.values())
    payload = json.loads(meta["card_payload"])
    assert set(payload) == {"title", "summary_rows", "approve_cta", "reject_cta"}   # no envelope
    assert payload["approve_cta"]["selection"] == {"action": "confirm", "request_id": "req-1"}
    assert payload["approve_cta"]["primary"] is True
    assert payload["reject_cta"]["selection"] == {"action": "cancel", "request_id": "req-1"}
    assert "Check and confirm:" in body(msg) and "Total: ₹4,200" in body(msg)


def test_carousel_fallback_lists_items():
    items = [
        cards.carousel_item("s1", "Shree Traders", "₹372/bag · 2 days", cards.cta("Choose", "choose", "r", supplier="s1"),
                            badges=[cards.badge_("Best value", "success")]),
        cards.carousel_item("s2", "Gupta Cement", "₹380/bag · 1 day", cards.cta("Choose", "choose", "r", supplier="s2")),
    ]
    card = cards.carousel("Quotes for 50 bags Cement", items)
    assert card.payload["items"][0]["primary_cta"]["selection"] == {"action": "choose", "request_id": "r", "supplier": "s1"}
    assert "1. Shree Traders [Best value]: ₹372/bag · 2 days" in card.fallback


def test_table_is_terminal_without_buttons():
    card = cards.table("Stock", ["Item", "Qty"], [["Cement", "12 bag"], ["Sand", "380 cft"]])
    assert card.terminal
    meta = metadata(cards.card_message(card))
    assert meta["is_terminal"] == "true"
    assert json.loads(meta["card_payload"])["root"]["type"] == "section"
    assert "Cement | 12 bag" in card.fallback


def test_table_with_row_buttons_needs_interaction():
    card = cards.table("Dues", ["Party", "Due"], [["Ramesh Kumar", "₹5,400"]],
                       row_buttons=[cards.button("Remind", "remind", party="Ramesh Kumar")])
    assert not card.terminal
    assert cards.depth(card.payload["root"]) <= cards.MAX_DEPTH


def test_too_deep_custom_degrades_to_text():
    el = cards.text("leaf")
    for _ in range(9):
        el = cards.group([el])
    msg = cards.card_message(cards.custom(el, "plain fallback"))
    assert metadata(msg) is None
    assert body(msg) == "plain fallback"


def test_oversize_payload_degrades_to_text():
    rows = [{"label": f"row {i}", "value": "x" * 200} for i in range(400)]
    msg = cards.card_message(cards.detail("Big", rows, []))
    assert metadata(msg) is None


@pytest.mark.parametrize("raw, expected", [
    ('{"action": "confirm", "request_id": "r1"}', {"action": "confirm", "request_id": "r1"}),
    ("haan", {"action": "confirm"}),
    ("OK!", {"action": "confirm"}),
    ("Theek hai", {"action": "confirm"}),
    ("Confirm the sale to Ramesh", {"action": "confirm"}),
    ("nahi", {"action": "cancel"}),
    ("Cancel", {"action": "cancel"}),
    ("Ramesh ko 10 bag cement becha", None),
    ('{"foo": 1}', None),
    ("[1, 2]", None),
])
def test_parse_selection(raw, expected):
    assert cards.parse_selection(raw) == expected
