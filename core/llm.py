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
from core.lang import DEFAULT, detect, name as lang_name, pick
from core.money import percent_of, to_paise

log = logging.getLogger("arth.llm")

Intent = Literal["sale", "purchase", "expense", "payment_in", "payment_out", "stock_query", "item_info",
                 "stock_adjust", "set_item", "set_party", "bill", "invoices", "cancel_bill", "dues", "remind",
                 "restock", "statement", "daybook", "question", "undo", "help"]
Mode = Literal["cash", "upi", "bank", "credit"]
# Insights reports a "question" can ask for; each is a fixed SQL report, so the model only picks one.
# "custom": none of the fixed reports fits, so Insights has ASI1 write a checked, read-only SQL query.
Report = Literal["summary", "top_items", "top_customers", "expenses", "compare", "gst", "purchases", "custom"]


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
    top: int | None = Field(None, ge=1)  # dues: "sabse zyada" -> 1, "top 3" -> 3
    payable: bool = False               # dues: what the shop owes suppliers ("mujhe kisko dena hai")
    older_than_days: int | None = Field(None, ge=1)   # dues: "15 din se purana udhaar"
    report: Report | None = None        # question: which Insights report answers it
    adjust: Literal["out", "in", "set"] | None = None   # stock_adjust: damaged/lost, found/extra, counted
    cost: float | None = None           # set_item: purchase cost per unit, rupees
    gst_rate: float | None = Field(None, ge=0, le=28)
    reorder_level: float | None = Field(None, ge=0)
    party_kind: Literal["customer", "supplier"] | None = None   # set_party
    phone: str | None = None            # set_party
    invoice: str | None = None          # invoices / cancel_bill: a bill number as spoken ("0003", "ARTH/26-27/0003")
    unpaid: bool = False                # invoices: only bills not fully paid
    pdf: bool = False                   # invoices: send the bill as a PDF
    follows_up: bool = False            # the message answers Arth's question about the earlier message
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
    "remind": [("party",)],
    "stock_adjust": [("lines",)],
    "set_item": [("lines",)],
    "set_party": [("party",)],
}

QUESTIONS = {
    "party": ("Kiske naam pe?", "Which customer or supplier?"),
    "lines": ("Kaunsa saamaan aur kitna?", "Which item, and how much?"),
    "amount": ("Kitne rupaye?", "What amount?"),
}


def compute_missing(ext: Extraction) -> list[str]:
    missing = []
    for options in REQUIRED.get(ext.intent, []):
        if not any(getattr(ext, f) not in (None, [], "") for f in options):
            missing.append(options[0] if len(options) == 1 else "/".join(options))
    return missing


def question_for(missing: list[str], lang: str = DEFAULT) -> str:
    return " ".join(pick(lang, *QUESTIONS[m.split("/")[0]]) if m.split("/")[0] in QUESTIONS
                    else pick(lang, f"{m} bataiye.", f"Please tell me the {m}.") for m in missing)


# Prompt

SYSTEM_PROMPT = """You turn messages from an Indian shop owner into JSON for a bookkeeping assistant.
Messages may be English, Hindi or Hinglish, with typos. Reply with ONE JSON object and nothing else.

Intents:
- sale: goods sold now, at the counter or to a named customer
- purchase: goods bought from a supplier (stock comes in)
- expense: shop spending: rent, salary, electricity, transport, chai, repairs
- payment_in: money received from a customer against what they owe
- payment_out: money paid to a supplier or anyone the shop owes
- stock_query: how much stock is left, or what is running low / finishing
- item_info: an item's selling price / rate, cost, margin or GST rate; with no item, the price list and
  the total value of stock
- stock_adjust: stock changes that are not a sale or purchase: damaged, broken, expired, lost, used in
  the shop (adjust "out"); found or returned to shelf (adjust "in"); a physical count ("asal mein 40
  bag hai", adjust "set", qty = the counted total)
- set_item: add a new item to the shop's list, or change an item's price, cost, GST rate, unit or
  reorder level
- set_party: add a new customer or supplier, or change their phone number
- bill: make a GST invoice / bill for a customer
- invoices: show bills already made: the last bill, a bill number, a customer's bills, this month's
  bills, unpaid bills; or send a bill as a PDF (pdf true)
- cancel_bill: cancel a bill that was already issued
- dues: lists and rankings of money owed: customers who owe the shop (udhaar, "lena hai", "who owes me")
  or, with payable true, people the shop owes ("dena hai", "who do I owe")
- remind: send a payment reminder to one named customer
- restock: order more stock from suppliers
- statement: one party's account / khata / hisaab / balance / phone: "Ramesh se kitna lena hai",
  "Gupta ko kitna dena hai", "Sunita ka number"
- daybook: the day's entries, today's hisaab, cash in hand, galla (period "today" unless another day is named)
- question: any other question about the business: sales, profit, expenses, best items or customers,
  growth, GST, suppliers, advice. Pick the report that answers it.
- undo: reverse or cancel the last entry
- help: greetings, "what can you do", or a message that is not about the shop at all

JSON shape (omit fields you don't know, never invent numbers):
{"intent": "...", "party": "name", "lines": [{"item": "name", "qty": 10, "unit": "bag", "rate": 420}],
 "amount": 4200, "paid": 2100, "paid_fraction": 0.5, "mode": "cash|upi|bank|credit",
 "discount_pct": 5, "category": "rent",
 "period": "today|yesterday|this_week|last_week|this_month|last_month|this_year|YYYY-MM|YYYY-MM-DD",
 "top": 1, "payable": true, "older_than_days": 30,
 "report": "summary|top_items|top_customers|expenses|compare|gst|purchases|custom",
 "adjust": "out|in|set", "cost": 380, "gst_rate": 18, "reorder_level": 20,
 "party_kind": "customer|supplier", "phone": "9876543210", "invoice": "0003", "unpaid": true, "pdf": true,
 "missing": ["party"]}

Rules:
- For a question (not an entry to record): use a fixed read intent (dues, statement, stock_query, item_info,
  invoices, daybook, or a named report) only when its fields can say ALL of the question. If the question
  adds anything they can't express (least / lowest / smallest, a threshold like "5000 se zyada", a count
  "kitne", a percentage, an average, a filter on mode, item, place or date range, a comparison of two
  parties), use {"intent": "question", "report": "custom"} instead, so the answer is computed exactly.
- amount, paid and rate are rupees exactly as the user said them. Do not do arithmetic.
- "half paid", "aadha diya" -> paid_fraction 0.5. Use paid only for an explicit paid amount.
- "udhaar", "baad mein dega", "credit" -> mode "credit". "online", "GPay", "PhonePe" -> mode "upi".
- Use the exact spelling from the shop's item and party lists when the user means one of them.
  If the user names someone not on the list, keep their spelling.
- Owners use local words for items: saria/sariya = TMT bar, eent/int = bricks, ret/reti = sand, bori = bag.
  If a word fits several shop items (sariya with no size), use the part their names share ("TMT Bar")
  so Arth can ask which one.
- Direction of money: "lena hai", "owes me", "udhaar baaki", "kisne nahi diya" = customers owe the shop.
  "dena hai", "I owe", "mujhe chukana hai", "supplier ka baaki" = the shop owes (payable true).
- dues: "sabse zyada", "most", "highest", "maximum" -> top 1; "top 3" -> top 3. "15 din se purana",
  "more than 30 days" -> older_than_days. A question about ONE named party's balance is statement, not dues.
- question report: overall sale/profit/"business kaisa hai" -> summary; best or slow sellers -> top_items;
  best customers, who buys most -> top_customers; kharcha, where money went -> expenses; growth,
  "badhi ya ghati", vs last month -> compare; GST collected or to pay -> gst; what was bought, which
  supplier, cheaper supplier, purchase rates -> purchases. Anything else answerable from the shop's records
  (averages, counts, a particular day or weekday, one customer's purchases of an item, the biggest single
  sale, cash vs UPI split, ...) -> custom.
- invoices: "last bill", "pichla bill" -> top 1; "last 5 bills" -> top 5; a bill number -> invoice.
  A PDF, file or download of a bill -> pdf true (with the bill number, or top 1 for the last bill).
  The bill of a sale already made ("Ramesh ki aaj wali sale ka bill", "today's walk-in sale ka bill dikhao")
  is also invoices, with party ("walk-in" for a counter sale) and period: Arth shows the bill, or makes one
  from that sale. Only a bill with items and quantities to sell now is intent bill.
- set_item: lines[0] is the item; its rate is the selling price; cost, gst_rate, reorder_level, unit
  only when said; qty only for opening stock of a new item.
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

"sabse zyada udhaar kiska baaki hai"
{"intent": "dues", "top": 1}

"mujhe kisko paise dene hain"
{"intent": "dues", "payable": true}

"Which customer owes me the most?"
{"intent": "dues", "top": 1}

"sabse kam kisko dena hai"
{"intent": "question", "report": "custom"}

"kaun se customers ka udhaar 10000 se upar hai"
{"intent": "question", "report": "custom"}

"Sunita ko paise ki yaad dila do"
{"intent": "remind", "party": "Sunita Devi"}

"Verma se kitna aana baaki hai"
{"intent": "statement", "party": "Verma Constructions"}

"aaj galle mein kitna cash hai"
{"intent": "daybook", "period": "today"}

"is mahine kitna profit hua"
{"intent": "question", "report": "summary", "period": "this_month"}

"average bill size kitna hai is mahine"
{"intent": "question", "report": "custom", "period": "this_month"}

"is saal sabse zyada maal kisne liya"
{"intent": "question", "report": "top_customers", "period": "this_year"}

"putty ka bhav kya chal raha hai"
{"intent": "item_info", "lines": [{"item": "Wall Putty 40kg"}]}

"3 bucket white paint leak ho gaye"
{"intent": "stock_adjust", "adjust": "out", "lines": [{"item": "White Paint 20L", "qty": 3, "unit": "bucket"}], "category": "damaged"}

"naya item: Tile Adhesive 20kg bag, 450 mein bechenge, 380 ka aata hai, GST 18"
{"intent": "set_item", "lines": [{"item": "Tile Adhesive 20kg", "unit": "bag", "rate": 450}], "cost": 380, "gst_rate": 18}

"Mohan Lal naya customer hai, number 9876543210"
{"intent": "set_party", "party": "Mohan Lal", "party_kind": "customer", "phone": "9876543210"}

"Sunita ke saare bill dikhao jo baaki hain"
{"intent": "invoices", "party": "Sunita Devi", "unpaid": true}

"bill number 7 cancel karo"
{"intent": "cancel_bill", "invoice": "7"}

"Verma wale bill ki PDF chahiye"
{"intent": "invoices", "party": "Verma Constructions", "top": 1, "pdf": true}

"Sunita ko aaj jo becha uska bill bhejo"
{"intent": "invoices", "party": "Sunita Devi", "period": "today", "pdf": true}
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


async def chat_json(messages: list[dict]) -> tuple[dict, str]:
    """One ASI1 call whose reply must hold a JSON object -> (the object, the raw reply). ValueError on bad JSON."""
    raw = await _chat(messages)
    return _json_object(raw), raw


def _finish(ext: Extraction) -> Extraction:
    ext.missing = compute_missing(ext)
    return ext


ANSWER_MAX_WORDS = 6    # offline only: a reply this short to Arth's question continues the earlier message


def _with_earlier(text: str, earlier: dict) -> str:
    return (f"Earlier message: {earlier['text']}\nArth asked: {earlier.get('question') or '(a follow-up question)'}\n"
            f"New message: {text}\n\n"
            "If the new message answers Arth's question, reply for the earlier request completed by the answer, "
            "with \"follows_up\": true. If it is a separate request, read only the new message.")


async def extract(text: str, shop: ShopContext, earlier: dict | None = None) -> Extraction:
    """Message -> validated Extraction. earlier = {"text", "question"} when Arth just asked something about a
    message; ASI1 decides whether this one answers it. One repair attempt on bad JSON, regex fallback if ASI1 is down."""
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT + "\n" + _shop_block(shop)},
        {"role": "user", "content": _with_earlier(text, earlier) if earlier else text},
    ]
    try:
        raw = await _chat(messages)
    except Exception as err:
        log.warning(f"ASI1 unavailable, using fallback: {type(err).__name__}: {err}")
        follows = bool(earlier) and len(text.split()) <= ANSWER_MAX_WORDS
        if (ext := fallback_extract(f"{earlier['text']}. {text}" if follows else text, shop)) is not None:
            ext.follows_up = follows
            return _finish(ext)
        raise ExtractionError(pick(detect(text), "Abhi samajh nahi paa raha, thodi der mein phir try kijiye.",
                                   "I can't reach my language service right now. Please try again shortly."))
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
    raise ExtractionError(pick(detect(text), "Maaf kijiye, samjha nahi. Thoda aur detail mein bataiye?",
                               "Sorry, I didn't get that. Could you say it another way?"))


STYLE = ("Write in {language}. Short, warm and clear, like a helpful munim talking to the owner: "
         "at most three short sentences, no headings, no bullet points, no emojis. Keep every figure exactly "
         "as given (with the ₹ sign and Indian commas); never compute, round or invent a number. Use correct "
         "singular and plural.")


async def explain(instruction: str, data: dict, temperature: float = 0.3, lang: str = DEFAULT) -> str:
    """Short prose from precomputed figures, in the owner's language. The model only words them."""
    return (await _chat([
        {"role": "system", "content": "You write replies for an Indian shop owner. " + STYLE.format(language=lang_name(lang))},
        {"role": "user", "content": f"{instruction}\n\nData:\n{json.dumps(data, ensure_ascii=False, default=str)}"},
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
    if re.search(r"\b(bank|cheque|check|neft|rtgs|imps|transfer)\b", low):
        return "bank"
    return "cash" if re.search(r"\b(cash|nakad|naqad)\b", low) else None


_QUESTION = re.compile(r"\b(profit|munafa|munafe|faayda|fayda|kamai|kamaya|business|report|kitni sale|sale kitni|"
                       r"kaisa raha|kaisa chal|kaisi chal|kitna bika|kitni bikri|total sale|sales)\b")
# Checked in order: the first report whose words appear answers the question.
_REPORTS = [
    ("compare", r"\b(compar\w*|mukabla|muqabla|badh[ia]?|ghat[ia]?|growth|vs)\b"),
    ("gst", r"\b(gst|tax)\b"),
    ("top_customers", r"\b(best customers?|top customers?|sabse (?:accha|achha|bada|zyada) (?:customer|grahak)|"
                      r"kisne sabse zyada|who buys most)\b"),
    ("top_items", r"\b(best sell\w*|top items?|most sold|kya (?:sabse )?zyada bik\w*|sabse zyada (?:kya )?bik\w*|"
                  r"kam bik\w*|sells? (?:the )?most|slow)\b"),
    ("purchases", r"\b(supplier|kharida|kharidi|purchases?|sasta|sabse sasta|cheap\w*)\b"),
    ("expenses", r"\b(kharch[ae]?|expenses?|spend|spent)\b"),
]
_PERIODS = [(r"\b(pichh?le|last) (mahine|month)\b", "last_month"), (r"\b(is|this) (mahine|month)\b", "this_month"),
            (r"\b(pichh?le|last) (hafte|week)\b", "last_week"), (r"\b(is|this) (hafte|week)\b", "this_week"),
            (r"\b(is|this) (saal|sal|year)\b", "this_year"),
            (r"\b(aaj|today)\b", "today"), (r"\b(kal|yesterday)\b", "yesterday")]
_ASKS = r"(kitna|kitne|kitni|kya|kaun|kaunsa|kis|kisko|kisne|kahan|kaise|kaisa|how|what|which|who|where|batao|dikhao|\?)"


def _period(low: str) -> str | None:
    return next((period for pattern, period in _PERIODS if re.search(pattern, low)), None)


_TOP = re.compile(r"\b(?:top\s*(\d+)|sabse\s+(?:zyada|jyada|zada|jada|adhik)|most|highest|maximum)\b")


def _top(low: str) -> int | None:
    m = _TOP.search(low)
    return (int(m.group(1)) if m.group(1) else 1) if m else None


_TRADE = re.compile(r"\b(becha|bechi|beche|bech|sold|bill|invoice|kharida|kharidi|khareeda|bought|mangwa\w*|"
                    r"aaya|aayi|aaye|liya|liye)\b")
# Devanagari words the rules below look for, spelt the way they are written in Hinglish.
_HINDI = {"हैं": "hain", "है": "hai", "किस किस": "kis kis", "लेना": "lena", "लेने": "lene", "देना": "dena", "देने": "dene", "उधार": "udhaar", "बाकी": "baaki",
          "बाक़ी": "baaki", "किसको": "kisko", "सबसे ज़्यादा": "sabse zyada", "सबसे ज्यादा": "sabse zyada",
          "मुनाफा": "munafa", "मुनाफ़ा": "munafa", "प्रॉफिट": "profit", "बिक्री": "bikri", "खर्चा": "kharcha",
          "स्टॉक": "stock", "बचा": "bacha", "कितना": "kitna", "कितने": "kitne", "बिल": "bill", "खाता": "khata",
          "हिसाब": "hisaab", "आज": "aaj", "इस महीने": "is mahine", "रेट": "rate", "भाव": "bhav"}
_EXPENSE = re.compile(r"\b(kiraya|kiraaya|rent|salary|tankhwah|pagaar|bijli|electricity|light bill|chai|nashta|"
                      r"transport|tempo|bhada|bhaada|petrol|diesel|repair|marammat)\b")
_CATEGORY = {"kiraya": "rent", "kiraaya": "rent", "tankhwah": "salary", "pagaar": "salary", "bijli": "electricity",
             "light bill": "electricity", "nashta": "chai", "tempo": "transport", "bhada": "transport",
             "bhaada": "transport", "petrol": "transport", "diesel": "transport", "marammat": "repair"}
# Words of a sale or purchase: a message with these is never a bare payment, even if the item name is misspelt.
_GOODS = re.compile(r"\b(becha|bechi|beche|bech|sold|khareeda|kharida|kharidi|bought|bags?|kg|pieces?|pcs|box|boxes|"
                    r"cft|bori|bucket)\b")


def _number(low: str) -> float | None:
    m = re.search(_NUM, low)
    return float(m.group(1).replace(",", "")) if m else None


def fallback_extract(text: str, shop: ShopContext) -> Extraction | None:
    """Ordered keyword rules: commands first, then questions about one party, lists, reports, items,
    and finally the demo's sale / bill / restock patterns. A message naming an item and a number with a
    trade verb is never read as a question, so "becha ... baaki udhaar" stays a sale."""
    low = text.lower().strip()
    for hindi, roman in _HINDI.items():
        low = low.replace(hindi, roman)
    discount = re.search(r"(\d+(?:\.\d+)?)\s*%\s*(?:discount|off|chhoot|chhut|kam)", low)
    if shop.draft and discount:
        return Extraction(intent="bill", discount_pct=float(discount.group(1)), source="fallback")
    if re.search(r"\b(help|madad|kya kar sakte|what can you do|hello|namaste)\b", low):
        return Extraction(intent="help", source="fallback")
    party = _find_party(text, shop.parties)
    full_number = re.search(r"\b[a-z]+/\d{2}-\d{2}/\d+\b", low)
    if (full_number or re.search(r"\b(bill|invoice)\b", low)) and re.search(r"\b(cancel|radd|rad)\b", low):
        number = full_number or re.search(r"\d+", low)
        return Extraction(intent="cancel_bill", source="fallback", party=party,
                          invoice=number.group(0) if number else None)
    if re.search(r"\b(undo|galti|galat|wapas lo|cancel last|pichli entry|last entry)\b", low):
        return Extraction(intent="undo", source="fallback")
    if re.search(r"\b(day ?book|cash in hand|galla|gall[ae]|(?:aaj|kal|today|yesterday) ka hisaab|kitna cash)\b", low):
        return Extraction(intent="daybook", source="fallback",
                          period="yesterday" if re.search(r"\b(kal|yesterday)\b", low) else "today")
    item = _find_item(text, shop.items)
    trade = bool(item and re.search(r"\d", low) and _TRADE.search(low))
    if not trade:
        if re.search(r"\b(bills?|invoices?)\b", low) and re.search(r"\b(dikha\w*|show|list|last|pichla|kitne|kaun\w*|"
                                                                   r"sab|baaki|unpaid|pdf)\b", low):
            last = re.search(r"\b(last|pichla|latest|aakhri)\b", low) and not re.search(r"\bbills\b", low)
            number = re.search(r"\b[a-z]+/\d{2}-\d{2}/\d+\b|(?:bill|invoice|no\.?|number|#)\s*(?:no\.?\s*)?(\d+)\b", low)
            pdf = bool(re.search(r"\bpdf\b", low))
            return Extraction(intent="invoices", source="fallback", party=party, period=_period(low),
                              invoice=(number.group(1) or number.group(0)) if number else None, pdf=pdf,
                              top=1 if last or (pdf and not number) else None,
                              unpaid=bool(re.search(r"\b(baaki|unpaid|pending)\b", low)))
        if party and re.search(r"\b(remind|reminder|yaad dila\w*)\b", low):
            return Extraction(intent="remind", party=party, source="fallback")
        if party and re.search(r"\b(statement|khata|khaata|hisaab|account|balance|number|phone|contact|"
                               r"lena|lene|dena|dene|aana|baaki|owes?)\b", low):
            return Extraction(intent="statement", party=party, source="fallback")
        if re.search(r"\b(kisko (?:\w+ ){0,2}den[aei]|den[ae] hai|dene hain|chukana|i owe|payables?)\b", low):
            return Extraction(intent="dues", source="fallback", top=_top(low), payable=True)
        if re.search(r"\b(dues|udhaa?r|baaki|kisne paise|owes?|lena hai|lene hain|paisa lena|paise lene|fasa|phansa|"
                     r"atka|not paid|haven't paid|nahi diy[ae])\b", low):
            days = re.search(r"(\d+)\s*(?:din|days?)", low)
            return Extraction(intent="dues", source="fallback", top=_top(low),
                              older_than_days=int(days.group(1)) if days else None)
        report = next((r for r, pattern in _REPORTS if re.search(pattern, low)), None)
        if (_QUESTION.search(low) or report in ("compare", "gst", "top_items", "top_customers")
                or (report and re.search(_ASKS, low))):
            # "pichle mahine se is mahine": a comparison is about the current period, against the one before
            period = (_period(low.replace("pichle", "").replace("last", "")) if report == "compare" else None)
            return Extraction(intent="question", source="fallback", report=report or "summary",
                              period=period or _period(low) or "this_month")
        if (re.search(r"\b(rate|price|bhav|bhaav|daam|dam|keemat|kimat|margin)\b", low)
                and not re.search(r"\b(naya|new|add)\b", low)):
            new_price = re.search(_NUM + r"\s*(?:kar|karo|set|rakh)", low)
            if item and new_price:
                return Extraction(intent="set_item", source="fallback",
                                  lines=[Line(item=item, rate=float(new_price.group(1).replace(",", "")))])
            return Extraction(intent="item_info", source="fallback", lines=[Line(item=item)] if item else [])
        if re.search(r"\b(stock|maal)\b", low) and re.search(r"\b(value|kitne ka|keemat)\b", low):
            return Extraction(intent="item_info", source="fallback")
        if item and re.search(r"\b(kharab|toot\w*|tut\w*|damaged?|broken|expired?|leak\w*|chori|lost)\b", low):
            return Extraction(intent="stock_adjust", source="fallback", adjust="out", category="damaged",
                              lines=[Line(item=item, qty=_number(low))])
        amount = _number(low)
        goods = item or _GOODS.search(low)
        if party and amount and not goods and re.search(r"\b(diye|diya|de diye|mile|mila|aaye|aaya|jama|received|"
                                                         r"paid|gpay|bheje|bheja)\b", low):
            # "Ramesh ne / se ... aaye" is money in; "Gupta ko ... diye", "paid ... to" is money out
            incoming = (re.search(r"\b(ne|from|received|mile|mila|aaye|aaya|jama)\b", low)
                        and not re.search(r"\b(ko|to|paid)\b", low))
            return Extraction(intent="payment_in" if incoming else "payment_out", party=party, amount=amount,
                              mode=_mode(low), source="fallback")
        if amount and not goods and (category := _EXPENSE.search(low)):
            word = category.group(1)
            return Extraction(intent="expense", amount=amount, category=_CATEGORY.get(word, word), mode=_mode(low),
                              source="fallback")
    if re.search(r"\b(restock|mangwa\w*|mangao|order|reorder)\b", low) and item:
        qty = re.search(_NUM + r"\s*(?:bag|bags|kg|piece|pcs|box|boxes)?", low)
        return Extraction(intent="restock", source="fallback",
                          lines=[Line(item=item, qty=float(qty.group(1).replace(",", "")) if qty else None)])
    if re.search(r"\b(stock|kitna bacha|kitne bache|kitna hai|khatam|low|left|bache|bachi)\b", low):
        return Extraction(intent="stock_query", source="fallback", lines=[Line(item=item)] if item else [])
    if re.search(r"\b(bill|invoice)\b", low) and item:
        qty = re.search(_NUM + r"\s*(?:bag|bags|kg|piece|pcs|box|boxes|cft|coil|bucket)?\s+"
                        + re.escape(item.split()[0].lower()), low)
        return Extraction(intent="bill", source="fallback", party=party,
                          lines=[Line(item=item, qty=float(qty.group(1).replace(",", "")) if qty else None)],
                          discount_pct=float(discount.group(1)) if discount else None,
                          paid_fraction=0.5 if re.search(r"\b(half|aadha|adha)\b", low) else None,
                          mode=_mode(low))
    if re.search(r"\b(becha|bech|sold|sale|diya)\b", low) and item:
        qty = re.search(_NUM + r"\s*(bag|bags|kg|piece|pcs|box|boxes|cft|coil|bucket)?\s+" + re.escape(item.split()[0].lower()), low)
        amount = re.search(r"(?:₹|rs\.?|rupees?)\s*" + _NUM + r"|" + _NUM + r"\s*(?:mein|me|rs|rupees?|₹)", low)
        return Extraction(
            intent="sale", source="fallback", party=party,
            lines=[Line(item=item, qty=float(qty.group(1).replace(",", "")) if qty else None,
                        unit=qty.group(2) if qty else None)],
            amount=float((amount.group(1) or amount.group(2)).replace(",", "")) if amount else None,
            paid_fraction=0.5 if re.search(r"\b(half|aadha|adha)\b", low) else None,
            mode=_mode(low))
    return None
