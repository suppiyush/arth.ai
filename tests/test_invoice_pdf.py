"""Invoice PDFs: the amount in words, the PDF itself, local and Cloudinary storage (faked), and the chat flow:
the PDF link with every issued bill (and no repeat of the bill card), and asking for an older bill's PDF."""

import json

import pytest

from test_orchestrator import (RAMESH_SALE, VERMA_BILL, balance, card, extracted, fy_number, io, say,  # noqa: F401
                               stock, texts)

from agents.billing import pdf
from core import config, storage
from core.llm import Extraction, Line


def test_amount_in_words_uses_lakhs_and_crores():
    assert pdf.amount_words(1957000) == "Rupees Nineteen Thousand Five Hundred Seventy Only"
    assert pdf.amount_words(12543050) == "Rupees One Lakh Twenty Five Thousand Four Hundred Thirty and Fifty Paise Only"
    assert pdf.number_words(23_00_00_000) == "Twenty Three Crore"


def test_render_makes_a_pdf_and_marks_cancelled_bills():
    inv = {"number": "ARTH/26-27/0001", "status": "issued", "issued_at": None, "discount_pct": 0,
           "taxable_paise": 1152542, "cgst_paise": 0, "sgst_paise": 0, "igst_paise": 207458, "round_off_paise": 0,
           "total_paise": 1360000}
    lines = [{"name": "TMT Bar 12mm", "hsn": "7214", "qty": 200, "unit": "kg", "rate_paise": 6800,
              "amount_paise": 1152542, "tax_paise": 207458, "gst_rate": 18}]
    shop = {"name": "Sharma Building Supplies", "gstin": "08ABCPS1234F1Z5", "state_code": "08", "upi_vpa": "s@upi"}
    party = {"name": "Verma Constructions", "phone": "919812067890", "state_code": "06"}
    data = pdf.render(inv, lines, shop, party)
    assert data.startswith(b"%PDF") and len(data) > 1000
    assert pdf.render({**inv, "status": "cancelled"}, lines, shop, None).startswith(b"%PDF")


def test_storage_names():
    assert storage.safe_name("ARTH/26-27/0001") == "ARTH-26-27-0001"


def issue_bill(io, extracted):
    extracted["result"] = VERMA_BILL
    draft = card(say("verma ka bill banao", io)[-1])["card_payload"]
    return say(json.dumps(draft["ctas"][0]["selection"]), io)[-1]


def test_without_cloudinary_the_pdf_is_saved_locally(seeded_db, io, extracted):
    done = issue_bill(io, extracted)
    assert "ka PDF is computer pe save hua" in texts([done])[0]
    saved = list(storage.LOCAL_DIR.rglob("*.pdf"))
    assert [p.name for p in saved] == ["ARTH-26-27-0001.pdf"] and saved[0].read_bytes().startswith(b"%PDF")
    assert seeded_db.execute("select pdf_url from invoice").fetchone()["pdf_url"] is None


SIGNED = "https://api.cloudinary.com/v1_1/demo/image/download?public_id={}&signature=x"


@pytest.fixture
def cloudinary(monkeypatch):
    """Cloudinary configured, with the upload and the signed-link builder replaced: records each upload."""
    uploads = []

    class Uploader:
        @staticmethod
        def upload(path, public_id, **opts):
            uploads.append((public_id, opts["resource_type"]))
            return {"secure_url": f"https://res.cloudinary.com/demo/image/upload/v1/{public_id}.pdf"}

    class Utils:
        @staticmethod
        def private_download_url(public_id, fmt, **opts):
            assert fmt == "pdf" and opts["expires_at"] > 0
            return SIGNED.format(public_id)

    class Fake:
        uploader, utils = Uploader, Utils

    monkeypatch.setattr(config, "CLOUDINARY_URL", "cloudinary://key:secret@demo")
    monkeypatch.setattr(storage, "_cloudinary", lambda: Fake)
    return uploads


def test_issued_bill_comes_with_just_the_pdf_link(seeded_db, io, extracted, cloudinary):
    done = issue_bill(io, extracted)
    url = SIGNED.format("arth/00000000-0000-4000-8000-000000000001/ARTH-26-27-0001")
    assert card(done) is None                                          # no second copy of the bill card
    assert texts([done])[0].endswith(f"📄 Bill {fy_number(1)} ka PDF: {url}")
    assert seeded_db.execute("select pdf_url from invoice").fetchone()["pdf_url"] == url
    assert cloudinary == [("arth/00000000-0000-4000-8000-000000000001/ARTH-26-27-0001", "image")]

    extracted["result"] = Extraction(intent="invoices", top=1, pdf=True)
    again = say("last bill ka pdf bhejo", io)[-1]                      # the stored link, no new upload
    assert texts([again]) == [f"📄 Bill {fy_number(1)} ka PDF: {url}"] and len(cloudinary) == 1


def test_an_old_public_link_is_replaced_by_a_signed_one(seeded_db, io, extracted, cloudinary):
    issue_bill(io, extracted)
    seeded_db.execute("update invoice set pdf_url = 'https://res.cloudinary.com/demo/image/upload/v1/old.pdf'")
    extracted["result"] = Extraction(intent="invoices", invoice="1", pdf=True)
    assert "/download?" in texts(say("bill 1 ka pdf bhejo", io))[-1] and len(cloudinary) == 2


def test_typed_pdf_request_and_cancelled_bill_is_redrawn(seeded_db, io, extracted, cloudinary):
    issue_bill(io, extracted)
    extracted["result"] = Extraction(intent="invoices", invoice="1", pdf=True)
    assert "ka PDF: https://" in texts(say("bill 1 ka pdf bhejo", io))[-1] and len(cloudinary) == 1

    extracted["result"] = Extraction(intent="cancel_bill", invoice="1")
    cancel = card(say("bill 1 cancel karo", io)[-1])["card_payload"]
    say(json.dumps(cancel["approve_cta"]["selection"]), io)
    assert seeded_db.execute("select pdf_url from invoice").fetchone()["pdf_url"] is None   # the old PDF isn't reused
    extracted["result"] = Extraction(intent="invoices", invoice="1", pdf=True)
    say("bill 1 ka pdf bhejo", io)
    assert len(cloudinary) == 2                                         # drawn again, now marked CANCELLED


# A bill for a sale already in the khata

def rows_of(reply):
    return {r["label"]: r["value"] for r in card(reply)["card_payload"]["summary_rows"]}


def test_bill_for_an_earlier_sale_is_made_from_it(seeded_db, io, extracted):
    extracted["result"] = RAMESH_SALE                                  # 10 bag cement, ₹4,200, half cash
    say("Ramesh ko 10 bag cement becha 4200 mein, aadha cash mila", io)
    say("confirm", io)
    assert balance(seeded_db, "Ramesh Kumar") == 750000 and stock(seeded_db, "Cement") == 2

    extracted["result"] = Extraction(intent="invoices", party="Ramesh Kumar", period="today", pdf=True)
    offer = say("Ramesh ki aaj wali sale ka bill bhejo", io)[-1]
    p = card(offer)["card_payload"]
    assert texts([offer])[0] == "Is sale ka bill abhi tak nahi bana. Ye bill bana dun?"
    assert p["title"] == "Ramesh Kumar ki sale ka GST bill" and p["approve_cta"]["label"] == "Bill banao"
    rows = rows_of(offer)
    assert rows["10 bag Cement @ ₹420"] == "₹4,200 (GST 28%)" and rows["Bill total"] == "₹4,200"
    assert "CGST" in rows and "IGST" not in rows                       # Ramesh is in Rajasthan, like the shop

    done = texts(say(json.dumps(p["approve_cta"]["selection"]), io))[-1]
    assert done.startswith("✅ Ho gaya!") and f"Bill {fy_number(1)} ban gaya." in done and "ka PDF" in done
    inv = seeded_db.execute("select * from invoice").fetchone()
    assert (inv["total_paise"], inv["status"]) == (420000, "partly_paid")
    sale = seeded_db.execute("select invoice_id from txn where kind = 'sale' and invoice_id is not null").fetchall()
    assert [r["invoice_id"] for r in sale] == [inv["id"]]
    paid = seeded_db.execute("select sum(amount_paise) as s from invoice_payment where invoice_id = %s", (inv["id"],)).fetchone()
    assert paid["s"] == 210000                                         # the half paid in cash at the sale
    assert balance(seeded_db, "Ramesh Kumar") == 750000 and stock(seeded_db, "Cement") == 2   # no second sale

    again = say("Ramesh ki aaj wali sale ka bill bhejo", io)[-1]       # now it exists: just the link
    assert card(again) is None and texts([again])[0].startswith(f"Bill {fy_number(1)} ka PDF")

    extracted["result"] = Extraction(intent="undo")
    undo = card(say("undo", io)[-1])["card_payload"]
    assert undo["title"] == "Undo: Ramesh Kumar ki sale ka GST bill?"
    text = texts(say(json.dumps(undo["approve_cta"]["selection"]), io))[-1]
    assert f"Bill {fy_number(1)} cancel ho gaya." in text
    assert seeded_db.execute("select count(*) as n from txn where invoice_id is not null").fetchone()["n"] == 0
    assert balance(seeded_db, "Ramesh Kumar") == 750000 and stock(seeded_db, "Cement") == 2   # the sale stays


def test_bill_for_a_walk_in_sale_splits_what_was_charged(seeded_db, io, extracted):
    extracted["result"] = Extraction(intent="sale", mode="cash", lines=[Line(item="Wall Putty 40kg", qty=2, unit="bag"),
                                                                       Line(item="Nails", qty=5, unit="kg")])
    say("walk-in ne 2 bag putty aur 5 kg keel liye cash", io)
    say("confirm", io)
    extracted["result"] = Extraction(intent="invoices", party="walk-in", period="today")
    p = card(say("aaj ki walk-in sale ka bill dikhao", io)[-1])["card_payload"]
    rows = {r["label"]: r["value"] for r in p["summary_rows"]}
    assert rows["Bill to"] == "Walk-in" and rows["Bill total"] == "₹2,250"
    assert rows["2 bag Wall Putty 40kg @ ₹850"] == "₹1,700 (GST 18%)" and rows["5 kg Nails @ ₹110"] == "₹550 (GST 18%)"
    say(json.dumps(p["approve_cta"]["selection"]), io)
    inv = seeded_db.execute("select party_id, status, total_paise from invoice").fetchone()
    assert (inv["party_id"], inv["status"], inv["total_paise"]) == (None, "paid", 225000)


def test_no_sale_to_bill(seeded_db, io, extracted):
    extracted["result"] = Extraction(intent="invoices", party="Sunita Devi", period="today", pdf=True)
    assert texts(say("Sunita ka aaj ka bill bhejo", io))[-1] == "Aaj Sunita Devi ki koi bina bill wali sale nahi mili."


def test_offline_pdf_request():
    from core import llm
    shop = llm.ShopContext(items=["Cement"], parties=["Verma Constructions"])
    ext = llm.fallback_extract("last bill ka pdf bhejo", shop)
    assert (ext.intent, ext.pdf, ext.top) == ("invoices", True, 1)
    assert llm.fallback_extract("bill 3 ki pdf", shop).invoice == "3"
