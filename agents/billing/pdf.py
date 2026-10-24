"""The GST tax invoice as a PDF (fpdf2, pure Python: no system libraries to install).

Everything printed comes from the invoice rows Billing already wrote; nothing is recomputed here except
adding up what the rows say. The core PDF fonts are Latin-1 only, so rupees are printed as "Rs." and
any other character a name might contain is replaced.
"""

from fpdf import FPDF

from core import config
from core.money import inr, qty

STATES = {"01": "Jammu & Kashmir", "02": "Himachal Pradesh", "03": "Punjab", "04": "Chandigarh", "05": "Uttarakhand",
          "06": "Haryana", "07": "Delhi", "08": "Rajasthan", "09": "Uttar Pradesh", "10": "Bihar", "19": "West Bengal",
          "23": "Madhya Pradesh", "24": "Gujarat", "27": "Maharashtra", "29": "Karnataka", "32": "Kerala",
          "33": "Tamil Nadu", "36": "Telangana", "37": "Andhra Pradesh"}
ONES = ["", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine", "Ten", "Eleven", "Twelve",
        "Thirteen", "Fourteen", "Fifteen", "Sixteen", "Seventeen", "Eighteen", "Nineteen"]
TENS = ["", "", "Twenty", "Thirty", "Forty", "Fifty", "Sixty", "Seventy", "Eighty", "Ninety"]


def _below_thousand(n: int) -> str:
    words = []
    if n >= 100:
        words += [ONES[n // 100], "Hundred"]
        n %= 100
    if n >= 20:
        words.append(TENS[n // 10])
        n %= 10
    if n:
        words.append(ONES[n])
    return " ".join(words)


def number_words(n: int) -> str:
    """Indian grouping: 1,25,430 -> "One Lakh Twenty Five Thousand Four Hundred Thirty"."""
    if n == 0:
        return "Zero"
    parts = []
    for size, name in ((10_000_000, "Crore"), (100_000, "Lakh"), (1000, "Thousand")):
        if n >= size:
            parts.append(f"{number_words(n // size) if n // size >= 1000 else _below_thousand(n // size)} {name}")
            n %= size
    if n:
        parts.append(_below_thousand(n))
    return " ".join(parts)


def amount_words(paise: int) -> str:
    rupees, rem = divmod(abs(paise), 100)
    text = f"Rupees {number_words(rupees)}"
    if rem:
        text += f" and {number_words(rem)} Paise"
    return text + " Only"


def rs(paise: int) -> str:
    return inr(paise, always_decimals=True).replace("₹", "Rs. ")


def _latin(text) -> str:
    return str(text if text is not None else "").replace("₹", "Rs. ").replace("—", "-").replace("–", "-") \
        .encode("latin-1", "replace").decode("latin-1")


class _Invoice(FPDF):
    def __init__(self, shop: str):
        super().__init__(format="A4")
        self.shop = shop
        self.set_auto_page_break(True, margin=18)
        self.set_margins(14, 14, 14)

    def footer(self):
        self.set_y(-14)
        self.set_font("Helvetica", "I", 8)
        self.set_text_color(120)
        self.cell(0, 6, _latin(f"Computer-generated invoice from {self.shop}, made with Arth. "
                               f"Page {self.page_no()}"), align="C")


def render(invoice: dict, lines: list[dict], shop: dict, party: dict | None) -> bytes:
    """invoice: an invoice row; lines: its invoice_line rows with item name, unit and hsn; shop: the business
    row; party: the customer row (or None for a deleted party)."""
    pdf = _Invoice(shop["name"])
    pdf.add_page()
    width = pdf.epw

    # Heading and the two parties
    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(width, 9, "TAX INVOICE", align="C", new_x="LMARGIN", new_y="NEXT")
    if invoice["status"] == "cancelled":
        pdf.set_text_color(200, 0, 0)
        pdf.set_font("Helvetica", "B", 13)
        pdf.cell(width, 7, "CANCELLED", align="C", new_x="LMARGIN", new_y="NEXT")
        pdf.set_text_color(0)
    pdf.ln(2)
    top = pdf.get_y()
    half = width / 2
    pdf.set_font("Helvetica", "B", 12)
    pdf.multi_cell(half - 4, 6, _latin(shop["name"]), new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 9)
    shop_state = STATES.get(shop.get("state_code") or "", "")
    for text in (f"GSTIN: {shop.get('gstin') or '-'}",
                 f"State: {shop_state} ({shop.get('state_code') or '-'})" if shop.get("state_code") else "",
                 f"UPI: {shop['upi_vpa']}" if shop.get("upi_vpa") else ""):
        if text:
            pdf.cell(half - 4, 5, _latin(text), new_x="LMARGIN", new_y="NEXT")
    left_bottom = pdf.get_y()

    pdf.set_xy(pdf.l_margin + half, top)
    issued = invoice.get("issued_at")
    party_state = (party or {}).get("state_code") or shop.get("state_code")
    for label, value in (("Invoice no", invoice["number"]),
                         ("Date", f"{issued:%d %b %Y}" if issued else "-"),
                         ("Place of supply", f"{STATES.get(party_state or '', '-')} ({party_state or '-'})")):
        pdf.set_x(pdf.l_margin + half)
        pdf.set_font("Helvetica", "B", 9)
        pdf.cell(32, 5, _latin(label))
        pdf.set_font("Helvetica", "", 9)
        pdf.cell(half - 32, 5, _latin(value), new_x="LMARGIN", new_y="NEXT")
    pdf.set_x(pdf.l_margin + half)
    pdf.set_font("Helvetica", "B", 9)
    pdf.cell(half, 6, "Bill to", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 9)
    bill_to = [(party or {}).get("name") or "Walk-in customer (cash sale)"]
    if party and party.get("phone") and len(party["phone"]) == 12:
        bill_to.append(f"Phone: +91 {party['phone'][2:]}")
    for text in bill_to:
        pdf.set_x(pdf.l_margin + half)
        pdf.cell(half, 5, _latin(text), new_x="LMARGIN", new_y="NEXT")
    pdf.set_y(max(left_bottom, pdf.get_y()) + 4)

    # Line items
    cols = [("#", 8, "C"), ("Item", 52, "L"), ("HSN", 16, "C"), ("Qty", 22, "R"), ("Rate*", 22, "R"),
            ("Taxable", 24, "R"), ("GST", 12, "R"), ("Amount", width - 156, "R")]
    pdf.set_fill_color(235, 235, 235)
    pdf.set_font("Helvetica", "B", 9)
    for name, w, align in cols:
        pdf.cell(w, 7, name, border=1, align=align, fill=True)
    pdf.ln()
    pdf.set_font("Helvetica", "", 9)
    for n, l in enumerate(lines, 1):
        cells = [str(n), l["name"], l.get("hsn") or "-", qty(l["qty"], l["unit"]), rs(l["rate_paise"]),
                 rs(l["amount_paise"]), f"{qty(l['gst_rate'])}%", rs(l["amount_paise"] + l["tax_paise"])]
        for (name, w, align), value in zip(cols, cells):
            pdf.cell(w, 7, _latin(value), border=1, align=align)
        pdf.ln()

    # Totals
    pdf.ln(2)
    label_w, value_w = 50, 36
    rows = []
    if invoice.get("discount_pct"):
        rows.append((f"Discount ({qty(invoice['discount_pct'])}%)", "included above"))
    rows.append(("Taxable value", rs(invoice["taxable_paise"])))
    if invoice.get("igst_paise"):
        rows.append(("IGST", rs(invoice["igst_paise"])))
    else:
        rows += [("CGST", rs(invoice.get("cgst_paise") or 0)), ("SGST", rs(invoice.get("sgst_paise") or 0))]
    if invoice.get("round_off_paise"):
        rows.append(("Round off", rs(invoice["round_off_paise"])))
    for label, value in rows:
        pdf.set_x(pdf.l_margin + width - label_w - value_w)
        pdf.cell(label_w, 6, label, align="R")
        pdf.cell(value_w, 6, value, align="R", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "B", 11)
    pdf.set_x(pdf.l_margin + width - label_w - value_w)
    pdf.cell(label_w, 8, "Total", border="T", align="R")
    pdf.cell(value_w, 8, rs(invoice["total_paise"]), border="T", align="R", new_x="LMARGIN", new_y="NEXT")

    pdf.ln(3)
    pdf.set_font("Helvetica", "", 9)
    pdf.multi_cell(width, 5, _latin(f"Amount in words: {amount_words(invoice['total_paise'])}"),
                   new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "I", 8)
    pdf.set_text_color(110)
    pdf.multi_cell(width, 4.5, "* Rates include GST; the taxable value and GST are worked out from them."
                   if config.PRICES_INCLUDE_GST else "* Rates are before GST.", new_x="LMARGIN", new_y="NEXT")
    return bytes(pdf.output())
