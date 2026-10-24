# Arth: the AI accountant for India's small shops

**The problem.** A kirana or building-materials shop runs on a paper *khata*: udhaar written on a notebook, GST bills made by hand, dues chased by memory, stock counted by eye. Accounting software asks the owner to learn forms and menus. Most never do.

**Arth's answer.** The owner just *says what happened*, in Hindi, English or Hinglish, in ASI:One chat:

> `Ramesh ko 10 bag cement becha 4200 mein, aadha cash mila`

Arth records the sale on Ramesh's khata, takes ₹2,100 cash in, reduces stock, warns that cement is running low, and waits for **one tap to confirm**. Ten Fetch.ai agents do the work behind that single chat.

## What it does

| | Feature | Try saying |
|---|---|---|
| 📒 | **Khata (ledger):** sales, purchases, expenses, part-payments, udhaar, party statements, day book with cash in hand | `Ramesh ka khata dikhao` · `aaj ka hisaab` |
| 📦 | **Inventory:** live stock, low-stock alerts, oversell warnings, damage and stock-count adjustments, rates and margins | `Cement kitna bacha hai?` · `2 bag cement kharab ho gaya` |
| 🧾 | **GST invoices:** CGST+SGST or IGST chosen by state, per-financial-year numbering, discounts, cancellation; a **PDF tax invoice** for every bill, stored on Cloudinary with a download link | `Bill bana do Verma ke liye, 200 kg TMT 12mm` · `Bill 1 ka PDF bhejo` |
| 💸 | **Collections:** who owes what and since when, one-tap reminders with a UPI pay link, a QR code and a ready-to-send WhatsApp message | `Kis kis ka udhaar baaki hai?` · `15 din se purana udhaar` |
| 🤝 | **Agent-to-agent restock:** three independent supplier agents quote, Arth **haggles** a counter-offer, the owner picks from a comparison carousel and pays in FET; Arth pays the supplier on-chain | `50 bag cement mangwa do` |
| 📊 | **Insights:** profit, top items and customers, month-on-month comparison, GST due, expenses, cheapest supplier | `Is mahine kitna profit hua?` · `Pichle mahine se sale badhi?` |
| 🧠 | **Ask anything:** questions no report covers are answered by SQL that ASI1 writes, run under strict guards | `Hafte ke kis din sabse zyada bikri?` · `Average bill size?` |
| ↩️ | **Undo:** reverses a whole action across every agent it touched | `undo` |

## Why it stands out

- **Built for Bharat, not translated for it.** Arth understands Hindi, English and Hinglish, including Devanagari, typos and shop words like *eent*, *ret* and *saria*, and replies in the language the owner used. "aadha cash mila" becomes `paid_fraction: 0.5`.
- **The LLM understands; code does the maths.** ASI1 only turns words into a checked intent. Every rupee, tax split and total is computed in code in integer paise, so there are no made-up numbers.
- **Nothing is written without the owner's yes.** Every change shows a review card first. Commits are idempotent, so a double-tap never counts twice, and Arth flags likely duplicate payments and payments larger than the amount due.
- **Open questions, safely.** For "ask anything", ASI1 writes SQL, and three guards keep it safe:
  - a parser allows only a single `SELECT` over six read-only views;
  - the query runs as a restricted database role that sees only this shop's data;
  - it runs in a read-only transaction with a 3-second timeout.
- **Real multi-agent economics, not a chatbot with tools.**
  - **Specialists:** an orchestrator coordinates six specialist agents (Ledger, Inventory, Billing, Collections, Procurement, Insights) through a plan → confirm → commit protocol under one action id.
  - **Suppliers:** Shree Traders, Gupta Cement and Balaji Build are separate seller agents that negotiate over uAgents messages.
- **Two-leg on-chain payments.** The owner pays Arth through ASI:One's *Approve FET Payment*, and Arth verifies the transfer on the Fetch.ai testnet. Procurement then pays the supplier, which verifies it in turn. If a supplier fails, the next-best one is used; if none can deliver, the owner is refunded automatically.
- **Keeps working when the LLM doesn't.** If ASI1 is unreachable or out of quota, a keyword fallback still handles dues, balances, stock, bills, reports and simple entries.

## Built on Fetch.ai

**uAgents** (10 agents, Mailbox) · **ASI1** (understanding and wording) · **ASI:One Interactive Cards** (review cards, tables, carousels, buttons) · **Chat Protocol** · **Agent Payment Protocol** with **testnet FET** on Dorado · Postgres · Cloudinary · fpdf2

## By the numbers

- **10 agents:** 1 orchestrator, 6 specialists, 3 supplier agents.
- **21 intents:** khata, stock, GST billing, collections, procurement, reports, settings and undo.
- **272 automated tests,** covering every money and tax rule and each conversation flow.
- **62-message language test set:** English, Hindi and Hinglish. The first 40 messages scored 40/40; the newer 22 haven't had a live run yet.
- **0 rupees** of arithmetic left to the LLM.

> **Arth means "wealth" and "meaning".** It gives a shopkeeper a full accounts team that speaks their language and starts working the moment they type.
