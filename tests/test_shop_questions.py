"""The everyday questions and edits beyond the demo: balances of one party, payables, aged dues, rates
and stock value, stock adjustments, item and party edits (with undo), bill lookups and cancelling an
older bill, and the Insights reports. Through respond(), with ASI1 replaced by fixed extractions."""

import json

import pytest

from test_orchestrator import (VERMA_BILL, balance, card, extracted, fy_number, io, say, stock,  # noqa: F401
                               texts)
from test_queries_undo import body_rows, confirm_last_card, section

from core import llm
from core.llm import Extraction, Line


@pytest.fixture
def no_asi1(monkeypatch):
    async def down(*_, **__):
        raise RuntimeError("ASI1 down")
    monkeypatch.setattr(llm, "explain", down)


def rows_of(reply):
    return {r["label"]: r["value"] for r in card(reply)["card_payload"]["summary_rows"]}


def issue_verma_bill(io, extracted):
    extracted["result"] = VERMA_BILL
    say("bill verma", io)
    say("confirm", io)


# Dues and balances

def test_who_do_i_owe_the_most(seeded_db, io, extracted):
    extracted["result"] = Extraction(intent="dues", payable=True, top=1)
    s = section(say("Who do I owe maximum amount?", io)[-1])
    assert s["subtitle"].startswith("You owe Shree Traders the most: ₹22,000.")       # asked in English


def test_dues_naming_one_party_shows_their_khata_not_a_reminder(seeded_db, io, extracted):
    extracted["result"] = Extraction(intent="dues", party="Ramesh Kumar")
    s = section(say("Ramesh se kitna lena hai?", io)[-1])
    assert s["title"] == "Khata: Ramesh Kumar" and s["subtitle"].startswith("Ramesh Kumar ka baaki: ₹5,400")
    assert seeded_db.execute("select count(*) as n from reminder").fetchone()["n"] == 0


def test_udhaar_from_today_reads_aaj_se(seeded_db, io, extracted):
    extracted["result"] = Extraction(intent="sale", party="Piyush Agarwal", paid=5000, mode="upi",
                                     lines=[Line(item="White Paint 20L", qty=3, unit="bucket")])
    confirm_last_card(io, say("Piyush Agarwal ko 3 bucket white paint becha, 5000 UPI se diye, baaki udhaar", io)[-1])
    extracted["result"] = Extraction(intent="dues")
    rows = {r[0]: r[1] for r in body_rows(say("kis kis se paisa lena hai", io)[-1])}
    assert rows["Piyush Agarwal"] == "₹8,350 baaki · aaj se" and rows["Ramesh Kumar"] == "₹5,400 baaki · 22 din se"


def test_paying_twice_warns_and_an_overpayment_is_an_advance(seeded_db, io, extracted):
    """Sunita owes ₹2,100: the first payment settles her; the same payment again is flagged twice over
    (nothing owed, and just recorded), and if confirmed it reads as an advance, never as a negative due."""
    extracted["result"] = Extraction(intent="payment_in", party="Sunita Devi", amount=2100, mode="upi")
    first = say("Sunita ne apna 2100 ka udhaar UPI se chuka diya", io)[-1]
    assert rows_of(first)["Baaki"] == "₹2,100 → ₹0" and card(first)["card_payload"]["approve_cta"]["label"] == "Confirm"
    confirm_last_card(io, first)

    again = say("Sunita ne apna 2100 ka udhaar UPI se chuka diya", io)[-1]
    rows = rows_of(again)
    assert rows["Baaki"] == "₹0 → ₹2,100 advance"
    assert rows["⚠ Baaki se zyada"] == "Sunita Devi ka sirf ₹0 baaki tha; ₹2,100 advance ban jayega"
    assert rows["⚠ Shayad dobara"].startswith("Yahi payment")
    assert card(again)["card_payload"]["approve_cta"]["label"] == "Phir bhi likho"
    done = texts(confirm_last_card(io, again))[-1]
    assert "Sunita Devi ka advance: ₹2,100." in done and "dena hai" not in done
    statement = section(say_statement(io, extracted, "Sunita Devi"))
    assert statement["subtitle"].startswith("Sunita Devi ka advance: ₹2,100")


def test_paying_a_supplier_more_than_owed_warns(seeded_db, io, extracted):
    extracted["result"] = Extraction(intent="payment_out", party="Shree Traders", amount=25000, mode="bank")
    review = say("Shree Traders ko 25000 bank se diye", io)[-1]
    rows = rows_of(review)
    assert rows["Aapko dena hai"] == "₹22,000 → ₹3,000 advance"
    assert rows["⚠ Baaki se zyada"] == "Aapko Shree Traders ko sirf ₹22,000 dena tha; ₹3,000 advance ho jayega"
    assert "Shree Traders ko advance diya hua: ₹3,000." in texts(confirm_last_card(io, review))[-1]


def say_statement(io, extracted, party):
    extracted["result"] = Extraction(intent="statement", party=party)
    return say(f"{party} ka khata dikhao", io)[-1]


def test_dues_older_than(seeded_db, io, extracted):
    extracted["result"] = Extraction(intent="dues", older_than_days=10)
    reply = say("10 din se purana udhaar", io)[-1]
    assert section(reply)["title"] == "10+ din purana udhaar"
    assert [r[0] for r in body_rows(reply)] == ["Verma Constructions", "Kailash Builders", "Meena Interiors", "Ramesh Kumar"]  # Sunita's is 9 days old
    extracted["result"] = Extraction(intent="dues", older_than_days=90)
    assert "90 din se purana koi udhaar nahi" in texts(say("90 din purana udhaar", io))[-1]


# Items: rates, stock value, adjustments, edits

def test_item_rate(seeded_db, io, extracted):
    extracted["result"] = Extraction(intent="item_info", lines=[Line(item="Cement")])
    answer = texts(say("cement ka rate kya hai", io))[-1]
    assert answer.startswith("Cement: bechne ka rate ₹420/bag")
    assert "kharid ₹380, margin ₹40 (10%)" in answer and "GST 28% (HSN 2523)" in answer and "Stock 12 bag" in answer


def test_rate_list_and_stock_value(seeded_db, io, extracted):
    extracted["result"] = Extraction(intent="item_info")
    reply = say("stock mein kitne ka maal hai", io)[-1]
    value = seeded_db.execute("select sum(stock_qty * cost_paise) as v from item").fetchone()["v"]
    from core.money import inr
    assert section(reply)["subtitle"].startswith(f"Stock ki value: {inr(int(value))} kharid rate pe")
    assert len(body_rows(reply)) == 12


def test_damaged_stock_then_undo(seeded_db, io, extracted):
    extracted["result"] = Extraction(intent="stock_adjust", adjust="out", category="damaged",
                                     lines=[Line(item="Cement", qty=2, unit="bag")])
    review = say("2 bag cement kharab ho gaya", io)[-1]
    assert card(review)["card_payload"]["title"] == "Stock badlav"
    assert rows_of(review) == {"Wajah": "damaged", "Stock: Cement": "12 → 10 bag", "Kharid rate pe value": "−₹760"}
    assert stock(seeded_db, "Cement") == 12
    done = texts(confirm_last_card(io, review))[-1]
    assert "Cement 10 bag bacha" in done and "⚠ Cement kam hai" in done
    assert stock(seeded_db, "Cement") == 10
    assert balance(seeded_db, "Ramesh Kumar") == 540000                  # no money moved

    extracted["result"] = Extraction(intent="undo")
    confirm_last_card(io, say("undo", io)[-1])
    assert stock(seeded_db, "Cement") == 12


def test_stock_count_sets_the_total(seeded_db, io, extracted):
    extracted["result"] = Extraction(intent="stock_adjust", adjust="set", lines=[Line(item="Sand", qty=400)])
    review = say("sand asal mein 400 cft hai", io)[-1]
    assert rows_of(review)["Stock: Sand"] == "380 → 400 cft"
    confirm_last_card(io, review)
    assert stock(seeded_db, "Sand") == 400
    extracted["result"] = Extraction(intent="stock_adjust", adjust="out", lines=[Line(item="Cement", qty=50)])
    assert "Stock mein sirf 12 bag Cement hai" in texts(say("50 bag cement toot gaye", io))[-1]


def test_change_price_then_undo(seeded_db, io, extracted):
    extracted["result"] = Extraction(intent="set_item", lines=[Line(item="Cement", rate=430)])
    review = say("cement ka price 430 kar do", io)[-1]
    assert card(review)["card_payload"]["title"] == "Cement badlein"
    assert rows_of(review) == {"Item": "Cement", "Bechne ka rate": "₹420 → ₹430"}
    done = texts(confirm_last_card(io, review))[-1]
    assert "Cement update ho gaya: Bechne ka rate ₹430." in done
    price = lambda: seeded_db.execute("select price_paise from item where name = 'Cement'").fetchone()["price_paise"]
    assert price() == 43000

    extracted["result"] = Extraction(intent="undo")
    done = texts(confirm_last_card(io, say("undo", io)[-1]))[-1]
    assert "Cement pehle jaisa." in done and price() == 42000


def test_new_item_asks_for_cost_then_adds_and_undo_removes(seeded_db, io, extracted):
    extracted["result"] = Extraction(intent="set_item", lines=[Line(item="Tile Adhesive 20kg", unit="bag", rate=450)])
    assert "kitne ka aata hai (kharid rate)" in texts(say("naya item tile adhesive 450", io))[-1]

    extracted["result"] = Extraction(intent="set_item", lines=[Line(item="Tile Adhesive 20kg", unit="bag", rate=450,
                                                                    qty=25)], cost=380, gst_rate=18)
    review = say("naya item tile adhesive 450, cost 380, 25 bag hai", io)[-1]
    rows = rows_of(review)
    assert rows["Naya item"] == "Tile Adhesive 20kg (per bag)" and rows["Kharid rate"] == "₹380"
    assert rows["GST"] == "18%" and rows["Shuru ka stock"] == "25 bag"
    done = texts(confirm_last_card(io, review))[-1]
    assert "Tile Adhesive 20kg item list mein jud gaya." in done and stock(seeded_db, "Tile Adhesive 20kg") == 25

    extracted["result"] = Extraction(intent="undo")
    confirm_last_card(io, say("undo", io)[-1])
    assert seeded_db.execute("select count(*) as n from item where name = 'Tile Adhesive 20kg'").fetchone()["n"] == 0


# Parties

def test_new_customer_then_undo(seeded_db, io, extracted):
    extracted["result"] = Extraction(intent="set_party", party="mohan lal", party_kind="customer", phone="98765 43210")
    review = say("Mohan Lal naya customer, 98765 43210", io)[-1]
    assert rows_of(review) == {"Naya customer": "Mohan Lal", "Phone": "+91 98765 43210"}
    assert "Mohan Lal ka khata khul gaya." in texts(confirm_last_card(io, review))[-1]
    row = seeded_db.execute("select kind, phone from party where name = 'Mohan Lal'").fetchone()
    assert (row["kind"], row["phone"]) == ("customer", "919876543210")

    extracted["result"] = Extraction(intent="undo")
    confirm_last_card(io, say("undo", io)[-1])
    assert seeded_db.execute("select count(*) as n from party where name = 'Mohan Lal'").fetchone()["n"] == 0


def test_change_phone_and_bad_number(seeded_db, io, extracted):
    extracted["result"] = Extraction(intent="set_party", party="Sunita Devi", phone="9000000001")
    review = say("Sunita ka number 9000000001 kar do", io)[-1]
    assert rows_of(review)["Phone"] == "+91 98280 54321 → +91 90000 00001"
    confirm_last_card(io, review)
    assert seeded_db.execute("select phone from party where name = 'Sunita Devi'").fetchone()["phone"] == "919000000001"
    extracted["result"] = Extraction(intent="set_party", party="Sunita Devi", phone="12345")
    assert "10 digit" in texts(say("Sunita ka number 12345", io))[-1]


# Bills

def test_last_bill_and_bill_list(seeded_db, io, extracted):
    extracted["result"] = Extraction(intent="invoices", top=1)
    assert texts(say("last bill dikhao", io))[-1] == "Abhi tak koi bill nahi bana."
    issue_verma_bill(io, extracted)

    extracted["result"] = Extraction(intent="invoices", top=1)
    detail = card(say("last bill dikhao", io)[-1])["card_payload"]
    rows = {r["label"]: r["value"] for r in detail["summary_rows"]}
    assert detail["title"] == f"Invoice {fy_number(1)}"
    assert rows["Bill to"] == "Verma Constructions" and rows["Bill total"] == "₹13,600"
    assert rows["200 kg TMT Bar 12mm @ ₹68"] == "₹13,600 (GST 18%)" and rows["Status"] == "Baaki (₹13,600 baaki)"

    extracted["result"] = Extraction(intent="invoices", party="Verma Constructions", unpaid=True)
    reply = say("verma ke baaki bills", io)[-1]                     # only one matches: that bill, with its PDF
    assert card(reply)["card_payload"]["title"] == f"Invoice {fy_number(1)}"
    assert f"Bill {fy_number(1)} ka PDF" in texts([reply])[0]

    issue_verma_bill(io, extracted)                                # with two, a list
    extracted["result"] = Extraction(intent="invoices", party="Verma Constructions", unpaid=True)
    reply = say("verma ke baaki bills", io)[-1]
    assert section(reply)["title"] == "Verma Constructions ke bills (baaki wale)"
    assert [r[0] for r in body_rows(reply)] == [fy_number(2), fy_number(1)]
    extracted["result"] = Extraction(intent="invoices", invoice="7")
    assert "Bill '7' nahi mila" in texts(say("bill 7 dikhao", io))[-1]


def test_cancel_an_older_bill_by_number(seeded_db, io, extracted):
    issue_verma_bill(io, extracted)
    extracted["result"] = Extraction(intent="expense", amount=100, category="chai", mode="cash")
    say("chai 100", io)
    say("confirm", io)                                   # the bill is no longer the latest action

    extracted["result"] = Extraction(intent="cancel_bill", invoice="1")
    ask = say("bill 1 cancel karo", io)[-1]
    p = card(ask)["card_payload"]
    assert p["title"] == f"Bill {fy_number(1)} cancel karein?" and p["approve_cta"]["label"] == "Bill cancel karo"
    done = texts(confirm_last_card(io, ask))[-1]
    assert f"Bill {fy_number(1)} cancel ho gaya." in done
    assert seeded_db.execute("select status from invoice").fetchone()["status"] == "cancelled"
    assert balance(seeded_db, "Verma Constructions") == 3600000 and stock(seeded_db, "TMT Bar 12mm") == 850
    kept = seeded_db.execute("select count(*) as n from txn where category = 'chai'").fetchone()["n"]
    assert kept == 1                                      # the later expense is untouched
    assert "pehle se cancel" in texts(say("bill 1 cancel karo", io))[-1]


# Insights reports (ASI1 down: the plain answer is what the owner sees)

def test_report_top_items(seeded_db, io, extracted, no_asi1):
    extracted["result"] = Extraction(intent="question", report="top_items")
    reply = say("sabse zyada kya bikta hai", io)[-1]
    first = texts([reply])[0]
    assert first.startswith("Is saal sabse zyada bika: TMT Bar 12mm")
    assert section(reply)["title"] == "Sabse zyada bikne wala maal: is saal"


def test_report_top_customers(seeded_db, io, extracted, no_asi1):
    extracted["result"] = Extraction(intent="question", report="top_customers")
    reply = say("best customer kaun hai", io)[-1]
    assert texts([reply])[0].startswith("Is saal sabse bade customer Verma Constructions hain: ₹61,000 ki kharid. "
                                        "Unka ₹36,000 abhi baaki hai.")
    assert section(reply)["children"][-1]["action"]["selection"]["action"] == "remind"


def test_report_expenses(seeded_db, io, extracted, no_asi1):
    extracted["result"] = Extraction(intent="question", report="expenses", period="this_year")
    reply = say("kharcha kahan gaya", io)[-1]
    assert texts([reply])[0].startswith("Is saal kul kharcha ₹63,800. Sabse bada: Rent (₹30,000).")
    assert [r[0] for r in body_rows(reply)] == ["Rent", "Salary", "Electricity", "Transport"]


def test_report_compare(seeded_db, io, extracted, no_asi1):
    extracted["result"] = Extraction(intent="question", report="compare", period="this_month")
    reply = say("pichle mahine se sale badhi ya ghati", io)[-1]
    assert texts([reply])[0].startswith("Bikri ")
    assert [r[0] for r in body_rows(reply)] == ["Bikri", "Gross profit", "Kharcha", "Net profit", "Kharid"]
    assert "%" in body_rows(reply)[0][-1]                       # 60 days of history: last month is complete


def test_compare_says_so_when_the_older_period_is_not_in_the_books(seeded_db, io, extracted, no_asi1):
    seeded_db.execute("delete from txn where txn_date < shop_today() - 5")    # a shop that started 5 days ago
    extracted["result"] = Extraction(intent="question", report="compare", period="last_week")
    reply = say("pichle hafte se sale badhi?", io)[-1]
    assert "tulna abhi nahi ho sakti" in texts([reply])[0]
    assert not any("%" in r[-1] for r in body_rows(reply))      # no made-up percentages


def test_report_gst(seeded_db, io, extracted, no_asi1):
    issue_verma_bill(io, extracted)
    extracted["result"] = Extraction(intent="question", report="gst", period="this_month")
    reply = say("is mahine GST kitna", io)[-1]
    answer = texts([reply])[0]
    assert "bills pe ₹2,074.58 (CGST ₹0, SGST ₹0, IGST ₹2,074.58)" in answer
    assert "input credit" in answer
    assert body_rows(reply)[0] == ["1 GST bill", "Bikri: ₹11,525.42 · GST: ₹2,074.58"]


def test_report_supplier_rates_for_an_item(seeded_db, io, extracted, no_asi1):
    extracted["result"] = Extraction(intent="question", report="purchases", lines=[Line(item="Cement")])
    reply = say("cement ka sasta supplier kaun", io)[-1]
    assert texts([reply])[0].startswith("Cement sabse sasta Gupta Cement (₹380/bag) se mila.")
    button = section(reply)["children"][-1]
    assert button["action"]["selection"] == {"action": "example", "text": "Cement mangwa do"}


def test_report_purchases_by_supplier(seeded_db, io, extracted, no_asi1):
    extracted["result"] = Extraction(intent="question", report="purchases")
    reply = say("kis supplier se kitna kharida", io)[-1]
    assert [r[0] for r in body_rows(reply)] == ["Shree Traders", "Jaipur Paints & Hardware", "Balaji Build",
                                                "Gupta Cement", "Rajasthan Sand Suppliers", "Mahaveer Hardware"]
    assert body_rows(reply)[0][1].endswith("Dena hai: ₹22,000")


# The offline rules (ASI1 unreachable) still understand the everyday questions

SHOP = llm.ShopContext(
    items=["Cement", "TMT Bar 12mm", "TMT Bar 8mm", "Bricks", "Sand", "Wall Putty 40kg", "White Paint 20L"],
    parties=["Ramesh Kumar", "Sunita Devi", "Verma Constructions", "Shree Traders", "Gupta Cement", "Balaji Build"])


def test_offline_never_turns_a_sale_into_a_payment():
    ext = llm.fallback_extract("Ramesh ko 10 bag cemnt becha 4200 me, aadha cash mila", SHOP)   # item misspelt
    assert ext is None or ext.intent == "sale"
    paid = llm.fallback_extract("Shree Traders ko 22000 de diye UPI se", SHOP)
    assert (paid.intent, paid.mode) == ("payment_out", "upi")
    assert llm.fallback_extract("tempo bhada 800 cash", SHOP).category == "transport"


@pytest.mark.parametrize("text, want", [
    ("Who do I owe maximum amount?", dict(intent="dues", payable=True, top=1)),
    ("Kis kis se paisa lena hai?", dict(intent="dues", payable=False)),
    ("kisko kitna dena hai", dict(intent="dues", payable=True)),
    ("मुझे किस किस से पैसे लेने हैं?", dict(intent="dues", payable=False)),
    ("which customers haven't paid for more than 15 days", dict(intent="dues", older_than_days=15)),
    ("Ramesh se kitna lena hai?", dict(intent="statement", party="Ramesh Kumar")),
    ("Ramesh ka phone number kya hai", dict(intent="statement", party="Ramesh Kumar")),
    ("Ramesh ko reminder bhej do", dict(intent="remind", party="Ramesh Kumar")),
    ("cement ka rate kya hai", dict(intent="item_info")),
    ("stock mein total kitne ka maal pada hai", dict(intent="item_info", lines=[])),
    ("cement ka price 430 kar do", dict(intent="set_item")),
    ("2 bag cement kharab ho gaya", dict(intent="stock_adjust", adjust="out")),
    ("last bill dikhao", dict(intent="invoices", top=1)),
    ("ARTH/26-27/0001 cancel kar do", dict(intent="cancel_bill", invoice="arth/26-27/0001")),
    ("Ramesh ne 2000 diye", dict(intent="payment_in", party="Ramesh Kumar", amount=2000)),
    ("Gupta ko 5000 diye", dict(intent="payment_out", party="Gupta Cement", amount=5000)),
    ("dukaan ka kiraya 15000 diya", dict(intent="expense", amount=15000)),
    ("kal ka hisaab dikhao", dict(intent="daybook", period="yesterday")),
    ("sabse zyada kya bikta hai", dict(intent="question", report="top_items")),
    ("who is my best customer", dict(intent="question", report="top_customers")),
    ("pichle mahine se is mahine sale badhi ya ghati", dict(intent="question", report="compare", period="this_month")),
    ("GST kitna bharna padega is mahine", dict(intent="question", report="gst")),
    ("pichli entry galat thi", dict(intent="undo")),
    ("Ramesh ko 10 bag cement becha 4200 mein, aadha cash, baaki udhaar", dict(intent="sale", party="Ramesh Kumar")),
])
def test_offline_understands(text, want):
    ext = llm.fallback_extract(text, SHOP)
    assert ext is not None, text
    for k, v in want.items():
        assert getattr(ext, k) == v, (text, k, getattr(ext, k))
