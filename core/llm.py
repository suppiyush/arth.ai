"""ASI1 (OpenAI-compatible) for language understanding: extract() turns a message into a validated
Extraction, explain() writes short prose from figures computed in SQL.

Arithmetic never comes from the model: it reports rupees as spoken and hints like paid_fraction,
and code turns those into paise. If ASI1 is unreachable, a regex fallback covers the demo patterns.
"""

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Literal

from openai import AsyncOpenAI
from pydantic import BaseModel, Field, ValidationError

from core import config
from core.money import percent_of, to_paise

log = logging.getLogger("arth.llm")

Intent = Literal["sale", "purchase", "expense", "payment_in", "payment_out", "stock_query", "bill",
                 "dues", "restock", "statement", "daybook", "question", "undo", "help"]
Mode = Literal["cash", "upi", "bank", "credit"]


class Line(BaseModel):
    item: str
    qty: float | None = None
    unit: str | None = None
    rate: float | None = None           # rupees per unit, only if the user said it


class Extraction(BaseModel):
    intent: Intent
    party: str | None = None
    lines: list[Line] = []
    amount: float | None = None         # rupees as spoken; converted to paise in code
    paid: float | None = None
    paid_fraction: float | None = Field(None, ge=0, le=1)   # "half paid" -> 0.5
    mode: Mode | None = None
    discount_pct: float | None = Field(None, ge=0, le=100)
    category: str | None = None         # expense category: rent, salary, electricity, transport, ...
    period: str | None = None           # "this_month", "today", "2026-10"
    missing: list[str] = []             # fields still needed before acting (recomputed in code)
    source: Literal["asi1", "fallback"] = "asi1"

    def lines_total_paise(self) -> int | None:
        """Sum of qty x rate when every line has both, else None (the agent prices from the catalogue)."""
        if not self.lines or any(l.qty is None or l.rate is None for l in self.lines):
            return None
        return sum(to_paise(l.qty * l.rate) for l in self.lines)

    def total_paise(self) -> int | None:
        if self.amount is not None:
            return to_paise(self.amount)
        return self.lines_total_paise()

    def paid_paise(self, total_paise: int) -> int | None:
        """Money received now. None means the user didn't say; the agent should ask."""
        if self.paid is not None:
            return min(to_paise(self.paid), total_paise)
        if self.paid_fraction is not None:
            return percent_of(total_paise, self.paid_fraction * 100)
        if self.mode == "credit":
            return 0
        if self.mode in ("cash", "upi", "bank"):
            return total_paise
        return None


class ExtractionError(Exception):
    """Could not get a valid extraction; .question is what to ask the user."""

    def __init__(self, question: str):
        super().__init__(question)
        self.question = question


@dataclass
class ShopContext:
    items: list[str] = field(default_factory=list)
    parties: list[str] = field(default_factory=list)
    draft: str | None = None        # e.g. "bill for Verma Constructions: 200 kg TMT Bar 12mm"


# Fields each intent cannot do without. The model's own `missing` is replaced by this check.
REQUIRED: dict[str, list[tuple[str, ...]]] = {
    "sale": [("lines", "amount")],
    "purchase": [("lines", "amount")],
    "expense": [("amount",)],
    "payment_in": [("party",), ("amount",)],
    "payment_out": [("party",), ("amount",)],
    "bill": [("party",), ("lines",)],
    "restock": [("lines",)],
    "statement": [("party",)],
}

QUESTIONS = {
    "party": "Kiske naam pe? (Which customer or supplier?)",
    "lines": "Kaunsa saamaan aur kitna? (Which item and how much?)",
    "amount": "Kitne rupaye? (What amount?)",
}


def compute_missing(ext: Extraction) -> list[str]:
    missing = []
    for options in REQUIRED.get(ext.intent, []):
        if not any(getattr(ext, f) not in (None, [], "") for f in options):
            missing.append(options[0] if len(options) == 1 else "/".join(options))
    return missing


def question_for(missing: list[str]) -> str:
    return " ".join(QUESTIONS.get(m.split("/")[0], f"Please tell me the {m}.") for m in missing)


# Prompt

SYSTEM_PROMPT = """You turn messages from an Indian shop owner into JSON for a bookkeeping assistant.
Messages may be English, Hindi or Hinglish, with typos. Reply with ONE JSON object and nothing else.

Intents:
- sale: goods sold now, at the counter or to a named customer
- purchase: goods bought from a supplier (stock comes in)
- expense: shop spending: rent, salary, electricity, transport, chai, repairs
- payment_in: money received from a customer against what they owe
- payment_out: money paid to a supplier or anyone the shop owes
- stock_query: how much stock is left, or what is running low
- bill: make a GST invoice / bill for a customer
- dues: who owes money, udhaar list, payment reminders
- restock: order more stock from suppliers
- statement: one party's account / khata / hisaab: their entries and balance
- daybook: the day's entries, today's hisaab, cash in hand, galla (period "today" unless another day is named)
- question: questions about the business: sales, profit, trends, reports, advice
- undo: reverse or cancel the last entry
- help: what can you do, greetings, unclear small talk

JSON shape (omit fields you don't know, never invent numbers):
{"intent": "...", "party": "name", "lines": [{"item": "name", "qty": 10, "unit": "bag", "rate": 420}],
 "amount": 4200, "paid": 2100, "paid_fraction": 0.5, "mode": "cash|upi|bank|credit",
 "discount_pct": 5, "category": "rent",
 "period": "today|yesterday|this_week|last_week|this_month|last_month|YYYY-MM|YYYY-MM-DD",
 "missing": ["party"]}

Rules:
- amount, paid and rate are rupees exactly as the user said them. Do not do arithmetic.
- "half paid", "aadha diya" -> paid_fraction 0.5. Use paid only for an explicit paid amount.
- "udhaar", "baad mein dega", "credit" -> mode "credit". "online", "GPay", "PhonePe" -> mode "upi".
- Use the exact spelling from the shop's item and party lists when the user means one of them.
  If the user names someone not on the list, keep their spelling.
- missing lists fields the intent needs that the user did not give.

Examples:
"Ramesh ko 10 bag cement becha 4200 mein, aadha cash mila"
{"intent": "sale", "party": "Ramesh Kumar", "lines": [{"item": "Cement", "qty": 10, "unit": "bag"}], "amount": 4200, "paid_fraction": 0.5, "mode": "cash"}

"Gupta Cement ko 5000 GPay kiye"
{"intent": "payment_out", "party": "Gupta Cement", "amount": 5000, "mode": "upi"}

"cement kitna bacha hai"
{"intent": "stock_query", "lines": [{"item": "Cement"}]}

"Make a bill for Verma Constructions, 200 kg TMT 12mm at 68, 5% discount"
{"intent": "bill", "party": "Verma Constructions", "lines": [{"item": "TMT Bar 12mm", "qty": 200, "unit": "kg", "rate": 68}], "discount_pct": 5}

"Ramesh ka khata dikhao"
{"intent": "statement", "party": "Ramesh Kumar"}

"aaj galle mein kitna cash hai"
{"intent": "daybook", "period": "today"}

"is mahine kitna profit hua"
{"intent": "question", "period": "this_month"}
"""


def _shop_block(shop: ShopContext) -> str:
    block = (f"Shop items: {', '.join(shop.items) or '(none)'}\n"
             f"Shop parties: {', '.join(shop.parties) or '(none)'}")
    if shop.draft:
        block += (f"\nOpen draft: {shop.draft}. A message that changes it (\"5% discount\", \"add 2 bags putty\", "
                  "\"make it 15 bags\", \"paid in cash\") is intent \"bill\" with ONLY the changed fields; "
                  "leave party out unless the owner names a different one.")
    return block


_clients: dict[int, AsyncOpenAI] = {}


def client() -> AsyncOpenAI:
    """One client per event loop: its connection pool is bound to the loop that created it."""
    loop = id(asyncio.get_running_loop())
    if loop not in _clients:
        _clients.clear()                # an old loop's client can't be reused
        _clients[loop] = AsyncOpenAI(api_key=config.require("ASI1_API_KEY"), base_url=config.ASI1_BASE_URL,
                                     timeout=15, max_retries=1)   # ASI1 quota is per request; don't spend it on retries
    return _clients[loop]


async def _chat(messages: list[dict], temperature: float = 0) -> str:
    if not config.ASI1_ENABLED:
        raise RuntimeError("ASI1 is switched off (ASI1_ENABLED=false)")
    resp = await client().chat.completions.create(model=config.ASI1_MODEL, messages=messages,
                                                  temperature=temperature)
    return resp.choices[0].message.content or ""


def _json_object(raw: str) -> dict:
    """Pull the first {...} out of a reply that may be wrapped in prose or ``` fences."""
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object in reply")
    return json.loads(raw[start:end + 1])


def _finish(ext: Extraction) -> Extraction:
    ext.missing = compute_missing(ext)
    return ext


async def extract(text: str, shop: ShopContext) -> Extraction:
    """Message -> validated Extraction. One repair attempt on bad JSON, regex fallback if ASI1 is down."""
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT + "\n" + _shop_block(shop)},
        {"role": "user", "content": text},
    ]
    try:
        raw = await _chat(messages)
    except Exception as err:
        log.warning(f"ASI1 unavailable, using fallback: {type(err).__name__}: {err}")
        if (ext := fallback_extract(text, shop)) is not None:
            return _finish(ext)
        raise ExtractionError("Abhi samajh nahi paa raha, thodi der mein phir try karein. "
                              "(I can't reach my language service right now, please try again shortly.)")
    for attempt in range(2):
        try:
            return _finish(Extraction.model_validate(_json_object(raw)))
        except (ValueError, ValidationError) as err:
            if attempt == 1:
                break
            messages += [
                {"role": "assistant", "content": raw},
                {"role": "user", "content": f"That was not valid: {err}. Reply with only the corrected JSON object."},
            ]
            try:
                raw = await _chat(messages)
            except Exception:
                break
    raise ExtractionError("Maaf kijiye, samjha nahi. Thoda aur detail mein bataiye? "
                          "(Sorry, I didn't get that. Could you say it another way?)")


async def explain(instruction: str, data: dict, temperature: float = 0.3) -> str:
    """Short prose from precomputed figures. The model only words them; it never computes."""
    return (await _chat([
        {"role": "system", "content": "You write short, plain replies for an Indian shop owner. "
                                      "Use only the figures given; never compute or invent numbers. "
                                      "Match the language of the owner's message (English, Hindi or Hinglish)."},
        {"role": "user", "content": f"{instruction}\n\nData:\n{json.dumps(data, ensure_ascii=False)}"},
    ], temperature)).strip()


# Fallback when ASI1 is down: just enough for the demo messages.

_NUM = r"(\d[\d,]*(?:\.\d+)?)"


def _find_item(text: str, items: list[str]) -> str | None:
    """Whole name in the text wins; else the item sharing the most words ("tmt 12mm" -> TMT Bar 12mm).
    A tie means we can't tell, so None."""
    low = text.lower()
    for name in sorted(items, key=len, reverse=True):
        if name.lower() in low:
            return name
    words = set(re.findall(r"[\w.]+", low))
    scored = sorted(((sum(w in words for w in name.lower().split()), name) for name in items), reverse=True)
    if scored and scored[0][0] and (len(scored) == 1 or scored[0][0] > scored[1][0]):
        return scored[0][1]
    return None


def _find_party(text: str, parties: list[str]) -> str | None:
    low = text.lower()
    for name in parties:
        if name.lower() in low or name.split()[0].lower() in low.split():
            return name
    return None


def _mode(low: str) -> str | None:
    if re.search(r"\b(upi|gpay|phonepe|online)\b", low):
        return "upi"
    if re.search(r"\b(udhaar|udhar|credit)\b", low):
        return "credit"
    return "cash" if "cash" in low else None


_QUESTION = re.compile(r"\b(profit|munafa|munafe|faayda|fayda|kamai|kamaya|business|report|kitni sale|sale kitni|"
                       r"kaisa raha|kaisa chal|kaisi chal)\b")
_PERIODS = [(r"\b(pichh?le|last) (mahine|month)\b", "last_month"), (r"\b(is|this) (mahine|month)\b", "this_month"),
            (r"\b(pichh?le|last) (hafte|week)\b", "last_week"), (r"\b(is|this) (hafte|week)\b", "this_week"),
            (r"\b(aaj|today)\b", "today"), (r"\b(kal|yesterday)\b", "yesterday")]


def _period(low: str) -> str | None:
    return next((period for pattern, period in _PERIODS if re.search(pattern, low)), None)


def fallback_extract(text: str, shop: ShopContext) -> Extraction | None:
    low = text.lower().strip()
    discount = re.search(r"(\d+(?:\.\d+)?)\s*%\s*(?:discount|off|chhoot|chhut|kam)", low)
    if shop.draft and discount:
        return Extraction(intent="bill", discount_pct=float(discount.group(1)), source="fallback")
    if re.search(r"\b(help|madad|kya kar sakte|hello|namaste)\b", low):
        return Extraction(intent="help", source="fallback")
    if re.search(r"\b(undo|galti|wapas lo|cancel last)\b", low):
        return Extraction(intent="undo", source="fallback")
    if re.search(r"\b(day ?book|cash in hand|galla|gall[ae]|aaj ka hisaab|kitna cash)\b", low):
        return Extraction(intent="daybook", source="fallback",
                          period="yesterday" if re.search(r"\b(kal|yesterday)\b", low) else "today")
    if _QUESTION.search(low):
        return Extraction(intent="question", source="fallback", period=_period(low) or "this_month")
    if re.search(r"\b(statement|khata|khaata|hisaab|account)\b", low) and (party := _find_party(text, shop.parties)):
        return Extraction(intent="statement", party=party, source="fallback")
    if re.search(r"\b(dues|udhaar|udhar|baaki|kisne paise|who owes|remind)\b", low):
        return Extraction(intent="dues", source="fallback")
    item = _find_item(text, shop.items)
    if re.search(r"\b(restock|mangwa|mangao|order kar|reorder)\b", low) and item:
        qty = re.search(_NUM + r"\s*(?:bag|bags|kg|piece|pcs|box|boxes)?", low)
        return Extraction(intent="restock", source="fallback",
                          lines=[Line(item=item, qty=float(qty.group(1).replace(",", "")) if qty else None)])
    if re.search(r"\b(stock|kitna bacha|kitne bache|kitna hai)\b", low):
        return Extraction(intent="stock_query", source="fallback", lines=[Line(item=item)] if item else [])
    if re.search(r"\b(bill|invoice)\b", low) and item:
        qty = re.search(_NUM + r"\s*(?:bag|bags|kg|piece|pcs|box|boxes|cft|coil|bucket)?\s+"
                        + re.escape(item.split()[0].lower()), low)
        return Extraction(intent="bill", source="fallback", party=_find_party(text, shop.parties),
                          lines=[Line(item=item, qty=float(qty.group(1).replace(",", "")) if qty else None)],
                          discount_pct=float(discount.group(1)) if discount else None,
                          paid_fraction=0.5 if re.search(r"\b(half|aadha|adha)\b", low) else None,
                          mode=_mode(low))
    if re.search(r"\b(becha|bech|sold|sale|diya)\b", low) and item:
        qty = re.search(_NUM + r"\s*(bag|bags|kg|piece|pcs|box|boxes|cft|coil|bucket)?\s+" + re.escape(item.split()[0].lower()), low)
        amount = re.search(r"(?:₹|rs\.?|rupees?)\s*" + _NUM + r"|" + _NUM + r"\s*(?:mein|me|rs|rupees?|₹)", low)
        return Extraction(
            intent="sale", source="fallback", party=_find_party(text, shop.parties),
            lines=[Line(item=item, qty=float(qty.group(1).replace(",", "")) if qty else None,
                        unit=qty.group(2) if qty else None)],
            amount=float((amount.group(1) or amount.group(2)).replace(",", "")) if amount else None,
            paid_fraction=0.5 if re.search(r"\b(half|aadha|adha)\b", low) else None,
            mode=_mode(low))
    return None
