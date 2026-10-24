"""The 62-message extraction set: real-sounding shop messages in English, Hindi and Hinglish (with
typos and local words), each with the fields ASI1 must get right. A case passes only on an exact
match of every listed field; fields not listed aren't checked.

    python -m tests.extraction_eval          # run against ASI1 and print a report (62 requests)

tests/test_extraction_live.py runs the same set under pytest and requires >= 90%. Failures feed the
prompt examples in core/llm.py; the messages here must not copy those examples.
"""

import asyncio
import sys
import time
from dataclasses import dataclass, field

from core import llm
from core.llm import Extraction, ShopContext

TARGET = 0.90
CONCURRENCY = 4

SHOP = ShopContext(
    items=["Cement", "TMT Bar 12mm", "TMT Bar 8mm", "Bricks", "Sand", "Wall Putty 40kg", "White Paint 20L",
           "PVC Pipe 1 inch", "Electric Wire 1.5mm", "Floor Tiles 2x2", "Nails", "Binding Wire"],
    parties=["Ramesh Kumar", "Sunita Devi", "Verma Constructions", "Shree Traders", "Gupta Cement", "Balaji Build"],
)
VERMA_DRAFT = "bill for Verma Constructions: 200 kg TMT Bar 12mm"


@dataclass
class Case:
    text: str
    want: dict                       # Extraction fields; "lines" is [(item, qty), ...], qty None = not given
    draft: str | None = None
    note: str = ""


CASES = [
    # sale
    Case("Ramesh ko 10 bag cemnt becha 4200 me, aadha cash mila",
         dict(intent="sale", party="Ramesh Kumar", lines=[("Cement", 10)], amount=4200, paid_fraction=0.5, mode="cash"),
         note="typo"),
    Case("sold 2 bags wall putty to sunita, she will pay later",
         dict(intent="sale", party="Sunita Devi", lines=[("Wall Putty 40kg", 2)], mode="credit")),
    Case("500 eent bechi 4500 cash", dict(intent="sale", lines=[("Bricks", 500)], amount=4500, mode="cash"),
         note="eent = bricks"),
    Case("रमेश को 5 बैग सीमेंट बेचा 2100 रुपये, पूरा UPI से",
         dict(intent="sale", party="Ramesh Kumar", lines=[("Cement", 5)], amount=2100, mode="upi"), note="Hindi"),
    Case("sunita ne 3 kg keel liye 330 ka, paise baad me dengi",
         dict(intent="sale", party="Sunita Devi", lines=[("Nails", 3)], amount=330, mode="credit"), note="keel = nails"),
    Case("walk in customer ko 20 cft ret becha 1100 me",
         dict(intent="sale", party=None, lines=[("Sand", 20)], amount=1100), note="ret = sand"),
    # purchase
    Case("Shree Traders se 100 kg TMT 12mm khareeda 6200 mein, udhaar",
         dict(intent="purchase", party="Shree Traders", lines=[("TMT Bar 12mm", 100)], amount=6200, mode="credit")),
    Case("bought 20 bags cement from gupta cement for 7600, paid by bank",
         dict(intent="purchase", party="Gupta Cement", lines=[("Cement", 20)], amount=7600, mode="bank")),
    Case("balaji se 2000 eent aayi 14000 ki",
         dict(intent="purchase", party="Balaji Build", lines=[("Bricks", 2000)], amount=14000)),
    # expense
    Case("dukaan ka kiraya 15000 diya", dict(intent="expense", amount=15000, category="rent")),
    Case("बिजली का बिल 2300 भरा", dict(intent="expense", amount=2300, category="electricity"), note="Hindi"),
    Case("tempo bhada 800 cash", dict(intent="expense", amount=800, mode="cash", category="transport")),
    Case("chai pani 120", dict(intent="expense", amount=120)),
    # payment_in
    Case("Verma se 10000 aaye bank transfer", dict(intent="payment_in", party="Verma Constructions", amount=10000, mode="bank")),
    Case("ramesh ne 2000 gpay kiya", dict(intent="payment_in", party="Ramesh Kumar", amount=2000, mode="upi")),
    Case("सुनीता जी ने 1000 नकद दिए", dict(intent="payment_in", party="Sunita Devi", amount=1000, mode="cash"), note="Hindi"),
    # payment_out
    Case("Shree Traders ko 22000 de diye UPI se", dict(intent="payment_out", party="Shree Traders", amount=22000, mode="upi")),
    Case("paid 5000 to balaji build by cheque", dict(intent="payment_out", party="Balaji Build", amount=5000, mode="bank"),
         note="cheque = bank"),
    # stock_query
    Case("cement kitna bacha hai?", dict(intent="stock_query", lines=[("Cement", None)])),
    Case("how many tmt 8mm left", dict(intent="stock_query", lines=[("TMT Bar 8mm", None)])),
    Case("stock dikhao", dict(intent="stock_query", lines=[])),
    # bill
    Case("bill bana do Ramesh ke liye 5 bag cement", dict(intent="bill", party="Ramesh Kumar", lines=[("Cement", 5)])),
    Case("Verma Constructions ka GST bill, 150 kg 12mm saria, 3% discount",
         dict(intent="bill", party="Verma Constructions", lines=[("TMT Bar 12mm", 150)], discount_pct=3),
         note="saria = TMT bar"),
    Case("sunita ka invoice banao 2 bucket white paint aur 1 bag putty",
         dict(intent="bill", party="Sunita Devi", lines=[("White Paint 20L", 2), ("Wall Putty 40kg", 1)])),
    Case("5% discount de do", dict(intent="bill", discount_pct=5, party=None), draft=VERMA_DRAFT, note="draft edit"),
    # dues
    Case("kis kis ka udhaar baaki hai", dict(intent="dues")),
    Case("who owes me money?", dict(intent="dues")),
    Case("Verma ko payment reminder bhejo", dict(intent="remind", party="Verma Constructions")),
    Case("Who do I owe maximum amount?", dict(intent="dues", payable=True, top=1)),
    Case("Who do I owe the least amount to?", dict(intent="question", report="custom"), note="twist on a fixed intent"),
    Case("how many customers owe me more than 2000", dict(intent="question", report="custom"),
         note="threshold and count"),
    Case("Kis kis se paisa lena hai?", dict(intent="dues", payable=False)),
    Case("मुझे किस किस को पैसे देने हैं?", dict(intent="dues", payable=True), note="Hindi"),
    Case("20 din se purana udhaar kiska hai", dict(intent="dues", older_than_days=20)),
    Case("Ramesh se kitna lena hai?", dict(intent="statement", party="Ramesh Kumar")),
    Case("how much do I owe gupta cement", dict(intent="statement", party="Gupta Cement")),
    # items
    Case("TMT 8mm ka rate batao", dict(intent="item_info", lines=[("TMT Bar 8mm", None)])),
    Case("dukan mein kul kitne ka stock hai", dict(intent="item_info", lines=[])),
    Case("5 bag putty mein seelan aa gayi, phekna padega",
         dict(intent="stock_adjust", adjust="out", lines=[("Wall Putty 40kg", 5)]), note="damaged"),
    Case("count kiya to bricks 5800 hi hain", dict(intent="stock_adjust", adjust="set", lines=[("Bricks", 5800)])),
    Case("sand ka rate 60 kar do", dict(intent="set_item", lines=[("Sand", None)])),
    Case("Rajesh Hardware ko supplier mein add karo, phone 9414099999",
         dict(intent="set_party", party="Rajesh Hardware", party_kind="supplier")),
    # bills already made
    Case("pichla bill dikhao", dict(intent="invoices", top=1)),
    Case("bill no 2 dikhana zara", dict(intent="invoices", invoice="2")),
    Case("verma ke kaunse bill baaki hain", dict(intent="invoices", party="Verma Constructions", unpaid=True)),
    Case("ARTH/26-27/0004 cancel kar do", dict(intent="cancel_bill")),
    # restock
    Case("50 bag cement order karo", dict(intent="restock", lines=[("Cement", 50)])),
    Case("cement khatam hone wala hai, mangwa do", dict(intent="restock", lines=[("Cement", None)])),
    Case("order 300 kg binding wire from suppliers", dict(intent="restock", lines=[("Binding Wire", 300)])),
    # statement
    Case("ramesh kumar ka hisaab kitab dikhao", dict(intent="statement", party="Ramesh Kumar")),
    Case("show shree traders account", dict(intent="statement", party="Shree Traders")),
    # daybook
    Case("aaj ka hisaab batao", dict(intent="daybook", period="today")),
    Case("kal galle mein kitna cash tha", dict(intent="daybook", period="yesterday")),
    # question
    Case("is mahine kitni sale hui", dict(intent="question", period="this_month")),
    Case("pichhle mahine ka profit kitna tha", dict(intent="question", period="last_month")),
    Case("which item sells the most?", dict(intent="question", report="top_items")),
    Case("mera sabse bada grahak kaun hai", dict(intent="question", report="top_customers")),
    Case("is mahine paisa kahan kahan kharch hua", dict(intent="question", report="expenses", period="this_month")),
    Case("last month ke comparison mein is month kaisa raha", dict(intent="question", report="compare")),
    Case("is mahine ka GST kitna banega", dict(intent="question", report="gst", period="this_month")),
    # undo, help
    Case("galti ho gayi, last entry hata do", dict(intent="undo")),
    Case("what can you do?", dict(intent="help")),
]


def mismatches(ext: Extraction, want: dict) -> list[str]:
    errors = []
    for key, value in want.items():
        if key == "lines":
            got = sorted((l.item, l.qty) for l in ext.lines)
            expected = sorted(value)
            if got != expected:
                errors.append(f"lines {got} != {expected}")
        elif getattr(ext, key) != value:
            errors.append(f"{key} {getattr(ext, key)!r} != {value!r}")
    if ext.source != "asi1":
        errors.append("answered by the offline fallback, not ASI1")
    return errors


@dataclass
class Result:
    case: Case
    errors: list[str] = field(default_factory=list)
    seconds: float = 0.0

    @property
    def ok(self) -> bool:
        return not self.errors


async def run_case(case: Case, limit: asyncio.Semaphore) -> Result:
    async with limit:
        started = time.monotonic()
        shop = ShopContext(SHOP.items, SHOP.parties, case.draft)
        try:
            ext = await llm.extract(case.text, shop)
            errors = mismatches(ext, case.want)
        except Exception as err:
            errors = [f"{type(err).__name__}: {err}"]
        return Result(case, errors, time.monotonic() - started)


async def run_all() -> list[Result]:
    limit = asyncio.Semaphore(CONCURRENCY)
    return await asyncio.gather(*(run_case(c, limit) for c in CASES))


def report(results: list[Result]) -> str:
    passed = sum(r.ok for r in results)
    lines = [f"Extraction: {passed}/{len(results)} = {passed / len(results):.0%} (target {TARGET:.0%})"]
    for r in results:
        if not r.ok:
            lines.append(f"  FAIL {r.case.text!r}" + (f" [{r.case.note}]" if r.case.note else ""))
            lines += [f"       {e}" for e in r.errors]
    lines.append(f"Slowest call: {max(r.seconds for r in results):.1f}s")
    return "\n".join(lines)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    print(report(asyncio.run(run_all())))
