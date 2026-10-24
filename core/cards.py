"""ASI:One Interactive Cards (card_protocol_version 1).

Every builder returns a Card holding the payload and a plain-text fallback. A card that fails validation
is sent as its fallback text alone. A valid card goes with one line of text (its narration, or its
title), so ASI:One doesn't show the same content twice; CARD_TEXT=full sends the whole fallback as well,
for chat clients that don't draw cards. Every CTA selection carries {"action": ..., "request_id": ...}.
"""

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4

from uagents_core.contrib.protocols.chat import ChatMessage, MetadataContent, TextContent

from core import config

MAX_PAYLOAD_BYTES = 64 * 1024
MAX_DEPTH = 8
KINDS = ("carousel", "detail", "form", "review", "custom")


@dataclass
class Card:
    kind: str
    payload: dict
    fallback: str
    terminal: bool = False          # informational only; no input expected


def selection(action: str, request_id: str | None = None, **extra) -> dict:
    sel = {"action": action, **extra}
    if request_id:
        sel["request_id"] = request_id
    return sel


def cta(label: str, action: str, request_id: str | None = None, primary: bool = False, **extra) -> dict:
    c = {"label": label, "selection": selection(action, request_id, **extra)}
    if primary:
        c["primary"] = True
    return c


def _rows_text(rows: list[dict]) -> str:
    return "\n".join(f"{r['label']}: {r['value']}" for r in rows)


def _ctas_text(ctas: list[dict]) -> str:
    return " / ".join(c["label"] for c in ctas)


# Predefined kinds

def review(title: str, rows: list[dict], request_id: str,
           approve_label: str = "Confirm", reject_label: str = "Cancel") -> Card:
    payload = {
        "title": title,
        "summary_rows": rows,
        "approve_cta": cta(approve_label, "confirm", request_id, primary=True),
        "reject_cta": cta(reject_label, "cancel", request_id),
    }
    # "confirm"/"cancel" are what parse_selection understands, whatever the button labels say
    fallback = f"{title}\n{_rows_text(rows)}\nReply \"confirm\" or \"cancel\"."
    return Card("review", payload, fallback)


def detail(title: str, rows: list[dict], ctas: list[dict], hero_image: str | None = None,
           sub_options: list[dict] | None = None) -> Card:
    payload = {"title": title, "summary_rows": rows, "ctas": ctas}
    if hero_image:
        payload["hero_image"] = hero_image
    if sub_options:
        payload["sub_options"] = sub_options
    fallback = f"{title}\n{_rows_text(rows)}"
    if ctas:
        fallback += f"\nOptions: {_ctas_text(ctas)}"
    return Card("detail", payload, fallback)


def carousel_item(id: str, title: str, subtitle: str, cta_: dict, secondary_text: str | None = None,
                  badges: list[dict] | None = None, image: str | None = None) -> dict:
    item = {"id": id, "title": title, "subtitle": subtitle, "primary_cta": cta_}
    if secondary_text:
        item["secondary_text"] = secondary_text
    if badges:
        item["badges"] = badges
    if image:
        item["image"] = image
    return item


def carousel(title: str, items: list[dict], subtitle: str | None = None) -> Card:
    payload = {"title": title, "items": items}
    if subtitle:
        payload["subtitle"] = subtitle
    lines = [title] + ([subtitle] if subtitle else [])
    for n, it in enumerate(items, 1):
        badge = f" [{', '.join(b['label'] for b in it.get('badges', []))}]" if it.get("badges") else ""
        extra = f", {it['secondary_text']}" if it.get("secondary_text") else ""
        lines.append(f"{n}. {it['title']}{badge}: {it['subtitle']}{extra}")
    lines.append("Reply with the name to choose.")
    return Card("carousel", payload, "\n".join(lines))


def badge_(label: str, variant: str = "info") -> dict:
    return {"label": label, "variant": variant}


# Custom element tree

def section(children: list[dict], title: str | None = None, subtitle: str | None = None) -> dict:
    el = {"type": "section", "children": children}
    if title:
        el["title"] = title
    if subtitle:
        el["subtitle"] = subtitle
    return el


def group(children: list[dict], direction: str = "column", gap: int | None = None) -> dict:
    el = {"type": "group", "direction": direction, "children": children}
    if gap is not None:
        el["gap"] = gap
    return el


def text(value: str, style: str | None = None) -> dict:
    el = {"type": "text", "value": value}
    if style:
        el["style"] = style
    return el


def heading(value: str, level: int = 2) -> dict:
    return {"type": "heading", "value": value, "level": level}


def badge(label: str, variant: str = "info") -> dict:
    return {"type": "badge", "label": label, "variant": variant}


def divider() -> dict:
    return {"type": "divider"}


def button(label: str, action: str, request_id: str | None = None, primary: bool = False, **extra) -> dict:
    el = {"type": "button", "label": label, "action": {"selection": selection(action, request_id, **extra)}}
    if primary:
        el["primary"] = True
    return el


def image(src: str, alt: str | None = None, aspect_ratio: str | None = None) -> dict:
    el = {"type": "image", "src": src}
    if alt:
        el["alt"] = alt
    if aspect_ratio:
        el["aspect_ratio"] = aspect_ratio
    return el


def custom(root: dict, fallback: str, terminal: bool = False) -> Card:
    return Card("custom", {"root": root}, fallback, terminal)


def table(title: str, headers: list[str], rows: list[list[str]], subtitle: str | None = None,
          row_buttons: list[dict | None] | None = None, footer: list[dict] | None = None,
          detail: Callable[[list[str]], str] | None = None) -> Card:
    """Rows as a list, because cards have no grid (a row of text pieces can't line up into columns):
    each row's first cell in bold, with row_buttons[i] beside it if set, and the other cells on a muted
    line below. detail(row) words that line; by default "Header: value · Header: value". The plain-text
    fallback keeps the columns."""
    def default(r: list[str]) -> str:
        return " · ".join(f"{h}: {v}" if h else v for h, v in zip(headers[1:], r[1:]) if v not in ("", "—"))

    items = []
    for i, r in enumerate(rows):
        head = text(r[0], "emphasis")
        if row_buttons and row_buttons[i]:
            head = group([head, row_buttons[i]], "row", gap=8)
        line = (detail or default)(r)
        items.append({"children": [head, text(line, "muted")] if line else [head]})
    children = [{"type": "list", "items": items}, *(footer or [])]
    fallback = "\n".join([title, *([subtitle] if subtitle else []), " | ".join(headers),
                          *(" | ".join(r) for r in rows)])
    has_input = bool(footer) or any(row_buttons or [])
    return custom(section(children, title, subtitle), fallback, terminal=not has_input)


# Validation and messages

def depth(el, level: int = 1) -> int:
    kids = el.get("children", []) if isinstance(el, dict) else []
    if isinstance(el, dict) and el.get("type") == "list":
        kids = [c for it in el.get("items", []) for c in it.get("children", [])]
    return max([level, *(depth(k, level + 1) for k in kids)])


def validate(card: Card) -> str:
    """Return the JSON payload string, or raise ValueError when ASI:One would reject the card."""
    if card.kind not in KINDS:
        raise ValueError(f"unknown card kind {card.kind!r}")
    if card.kind == "custom" and depth(card.payload["root"]) > MAX_DEPTH:
        raise ValueError(f"custom card deeper than {MAX_DEPTH} levels")
    raw = json.dumps(card.payload, ensure_ascii=False, separators=(",", ":"))
    if len(raw.encode("utf-8")) > MAX_PAYLOAD_BYTES:
        raise ValueError("card payload over 64 KB")
    return raw


def text_message(body: str) -> ChatMessage:
    return ChatMessage(timestamp=datetime.now(timezone.utc), msg_id=uuid4(),
                       content=[TextContent(type="text", text=body)])


def card_message(card: Card, narration: str = "", drawer_width_px: int | None = None) -> ChatMessage:
    """A line of text, then the card. An invalid card degrades to its whole fallback as text."""
    full = f"{narration}\n\n{card.fallback}" if narration else card.fallback
    try:
        payload = validate(card)
    except ValueError:
        return text_message(full)
    body = full if config.CARD_TEXT == "full" else narration or card.fallback.split("\n", 1)[0]
    metadata = {
        "card_protocol_version": "1",
        "requires_card_interaction": "true",
        "card_kind": card.kind,
        "card_payload": payload,
    }
    if card.terminal:
        metadata["is_terminal"] = "true"
    if drawer_width_px:
        metadata["preferred_drawer_width_px"] = str(drawer_width_px)
    return ChatMessage(timestamp=datetime.now(timezone.utc), msg_id=uuid4(), content=[
        TextContent(type="text", text=body),
        MetadataContent(type="metadata", metadata=metadata),
    ])


# Selections coming back

CONFIRM_WORDS = {"confirm", "confirmed", "yes", "haan", "han", "ha", "ok", "okay", "theek hai", "thik hai",
                 "done", "approve", "approved", "kar do", "karo"}
CANCEL_WORDS = {"cancel", "no", "nahi", "nahin", "mat karo", "reject", "rehne do", "stop"}


def parse_selection(raw: str) -> dict | None:
    """Direct @mention sends the selection as JSON; planner-mediated sends prose.

    Returns the selection dict, {"action": "confirm"|"cancel"} for a bare confirm/cancel word,
    or None when the text is a new request.
    """
    raw = raw.strip()
    try:
        value = json.loads(raw)
        if isinstance(value, dict) and "action" in value:
            return value
    except json.JSONDecodeError:
        pass
    words = re.sub(r"[^\w\s]", " ", raw.lower()).strip()
    words = re.sub(r"\s+", " ", words)
    if words in CONFIRM_WORDS or words.startswith(("confirm", "approve")):
        return {"action": "confirm"}
    if words in CANCEL_WORDS or words.startswith("cancel"):
        return {"action": "cancel"}
    return None
