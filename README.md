# Arth: an AI accountant for India's small shops

![tag:innovationlab](https://img.shields.io/badge/innovationlab-3D8BD3)
![tag:hackathon](https://img.shields.io/badge/hackathon-5F43F1)

Built for **Fetch-A-Thon 3.0** with Fetch.ai uAgents, ASI1, ASI:One and the Agent Payment Protocol.

## The problem

India has millions of small shops (kiranas, hardware and building-material stores) that still run on a paper *khata*.
- **Credit (udhaar)** is written in a notebook and chased from memory, so money owed slips through the cracks.
- **GST bills** are made by hand, and tax splits are often wrong.
- **Stock** is counted by eye, so items run out without warning.
- **Restocking** means phoning suppliers one by one and haggling on every order.

Accounting software exists, but it expects the owner to learn forms, menus and accounting terms. Most shopkeepers never do, so the shops with the least time for bookkeeping are the ones left without it.

## What Arth does

Arth is an AI accountant that lives in chat on **ASI:One**. The owner just says what happened at the counter, in **Hindi, English or Hinglish**. For example: *"Ramesh ko 10 bag cement becha 4200 mein, aadha cash mila"*. Arth turns that into the right entries and shows a review card, and **nothing is saved until the owner taps Confirm**.

Behind that one chat, a team of Fetch.ai agents does the work:
- **Khata:** keeps the books and tracks stock.
- **GST bills:** issues proper invoices as PDFs.
- **Dues:** chases them with WhatsApp and UPI reminders.
- **Questions:** answers anything about the business from the shop's real numbers.
- **Restocking:** gets quotes from **supplier agents**, haggles, and pays them in testnet FET.

## Architecture

![Arth architecture](assets/architecture.png)

## Components

### Talking to Arth

**Shop owner.** Types what happened in English, Hindi or Hinglish, answers Arth's follow-up questions ("TMT Bar 12mm ya 8mm?"), and confirms each entry with one tap. There's no app to install and no form to fill.

**ASI:One chat and Interactive Cards.** ASI:One is where the owner talks to Arth, over Fetch.ai's Chat Protocol. Replies come as rich cards: review cards with Confirm and Cancel, dues and stock lists, a supplier comparison carousel, and Remind buttons.

**Approve FET Payment.** ASI:One's built-in payment prompt. When the owner approves a restock, their ASI:One wallet pays Arth in testnet FET through the Agent Payment Protocol.

**Agentverse Mailbox.** Relays messages between ASI:One and the Arth orchestrator, so Arth is listed and reachable on Agentverse without a public web address.

### Arth's agents (Fetch.ai uAgents)

**Arth Orchestrator.** The only agent the owner talks to. It detects the owner's language, has ASI1 understand the message, and asks back when a detail is missing. It then has the right specialists plan the change and shows one review card. On Confirm it has them commit together under one action id, so `undo` can reverse the whole thing.

**Ledger.** The khata: sales, purchases, expenses and payments in and out, with each party's balance, statements and the day book. It warns about likely duplicate payments and payments larger than what is owed.

**Inventory.** Moves stock on every sale and purchase, warns before overselling, and flags items below their reorder level. It also records damaged or recounted stock, and adds or edits items with their rate, cost and GST.

**Billing.** Issues GST invoices, with CGST and SGST within the state or IGST across states, numbered per financial year. It handles discounts typed in chat and cancellations. Every bill is drawn as a PDF tax invoice.

**Collections.** Knows who owes what and for how long, and what the shop owes its suppliers. It drafts polite reminders with a UPI pay link, a QR code and a ready-to-send WhatsApp message.

**Procurement.** Asks the supplier agents for quotes, counters the two cheapest at 2% less, and shows the owner a comparison. After the owner pays, it pays the chosen supplier from its own wallet. If a supplier can't deliver it tries the next one, and refunds the owner if none can.

**Insights.** Reports on sales, profit, best sellers and best customers, month-on-month change, GST due and expenses. Any other question goes to ASI1-written SQL, so it's answered from the books.

### Intelligence and data

**ASI1.** Fetch.ai's language model. It turns a message into a checked intent ("aadha cash mila" becomes half paid, "sariya" becomes TMT Bar) and words replies in the owner's language. Every rupee, tax split and total is computed in code, never by the model.

**Keyword fallback.** If ASI1 is unreachable or out of quota, a rule-based reader still handles the common messages: dues, balances, stock, bills, reports and simple entries.

**Postgres.** One shared database holds the books, with all money stored as whole paise. Each agent writes only its own tables.

**SQL guard.** ASI1-written queries are checked with sqlglot: one SELECT, over six read-only views of this shop's data only. They then run under a read-only role with a 3-second time limit, and the figures in the answer come from the query result.

### Outside services

**Cloudinary.** Hosts every invoice PDF, and Arth posts its download link in the chat.

**WhatsApp and UPI.** A reminder becomes a WhatsApp link with the message already typed. It carries a UPI payment link for the exact amount owed, plus a QR code on the card.

### Supplier agents and payments

**Supplier agents.** Shree Traders, Gupta Cement and Balaji Build are three independent uAgents, each with its own price list, floor price, stock and wallet. They answer requests for quotes, accept or refuse one counter-offer, and confirm orders: an agent economy in miniature.

**Fetch.ai Dorado testnet.** Restock payments are real testnet FET transfers in two legs, each checked on-chain:
1. The owner pays the Orchestrator's wallet (the order plus a 1% Arth fee).
2. The Procurement wallet pays the supplier.

If no supplier can deliver, the Procurement wallet refunds the owner.

## Try saying

| Message | What happens |
|---|---|
| `Ramesh ko 10 bag cement becha 4200 mein, aadha cash mila` | Review card: sale on Ramesh's khata, ₹2,100 cash received, stock down, low-stock warning |
| `Kailash Builders ko 200 kg sariya becha, udhaar mein` | Arth asks "TMT Bar 12mm ya TMT Bar 8mm?", and a one-word answer continues the sale |
| `Verma Constructions ka bill banao: 200 kg TMT 12mm aur 100 kg TMT 8mm` | GST invoice with IGST (Verma is in Haryana); `5% discount do` edits it; Confirm gives a PDF link |
| `Kis kis se paisa lena hai?` / `Who do I owe the most?` | Dues list with how long each is owed and a Remind button; payables ranked |
| `Verma ko payment reminder bhejo` | Polite message with a UPI link and QR code, then a ready-to-send WhatsApp link |
| `Cement kitna bacha hai?` / `2 bag cement kharab ho gaya` | Stock level and reorder warning; a stock write-off card |
| `इस महीने कितना मुनाफा हुआ?` / `Pichle mahine se sale badhi?` | Sales, gross and net profit, dues, and a month-on-month comparison |
| `Hafte ke kis din sabse zyada bikri hoti hai?` | A question with no ready-made report: ASI1 writes a checked, read-only SQL query |
| `50 bag cement mangwa do` | Three supplier agents quote, Arth haggles, the owner picks and pays in FET, and Arth pays the supplier |
| `undo` | Reverses the last confirmed entry across every agent it touched |

The demo shop is *Sharma Building Supplies* in Jaipur, with 12 items, 15 customers and suppliers, and two months of history. The first ASI:One account to message Arth owns it. Everyone after that, judges included, gets a private copy of it.

## Agents

| Agent | Role | Address |
|---|---|---|
| **Arth** (orchestrator) | The one ASI:One talks to: chat, cards, owner payments | `agent1qfw2edq9jy9han0886rjwzea87phw3u74pdxxyzt2u8fc8mcw5aakl9dm73` |
| Arth Ledger | Khata: sales, purchases, expenses, payments, statements, day book | `agent1qvzwvc9xhjghaycnrv2as620sm2jt2des4dxm6cp32jmh86ptjy82ft7vyj` |
| Arth Inventory | Stock moves, oversell checks, low-stock alerts | `agent1qf822ktealykua0ygxh2quaqv34rqsdqcwxk5r4q5tu2z6y84dr57m8n002` |
| Arth Billing | GST invoices, numbering and PDFs | `agent1qws5k9v60rzkkxjte4t7sskchah46et66knw7py8c68x5zlk8fhjk8g84wg` |
| Arth Collections | Dues, UPI reminders, receipts against invoices | `agent1q25l3r70jttjqcxzcyhhlnh298psxjaa4nkkz2xhnpxmeq9qfcf6zj3h5ek` |
| Arth Procurement | Quotes, negotiation, paying suppliers | `agent1qtvef83la8y472mgkzw9dwcaa03fd055rw6kumfg0jfvku93w4als6j54v7` |
| Arth Insights | Reports and ask-anything answers from SQL | `agent1qdajans5hypzpwcnuf7scww4sqk602nhtdzljvcunw48f0ljs088ytem77e` |
| Shree Traders | Supplier agent (independent seller) | `agent1qdw223f5sepwtc2qg0ht9mngp72qfe6z2y2j430sq79k4wc0ztkfgc4gy93` |
| Gupta Cement | Supplier agent (independent seller) | `agent1q2uap68ng3g2qazpac88yhv9w9q9e48jhngr7aeucz0jaelhxpe9qv8c2tf` |
| Balaji Build | Supplier agent (independent seller) | `agent1qt2ypyvw65ndeswjpfx63y72wv7eavn53ef54uj2dzf9dxvqkw29vvh9hrf` |

Addresses come from the seeds in `.env`; new seeds give new addresses.

## Tech stack

Python · uAgents 0.25.5 / uagents-core 0.4.9 (Chat Protocol, Payment Protocol) · ASI1 · ASI:One Interactive Cards · Postgres with psycopg 3 · sqlglot · cosmpy (testnet FET) · fpdf2 + Cloudinary (invoice PDFs) · rapidfuzz · qrcode · pytest · Docker.

## Run it locally

1. **Python and dependencies** (Python 3.11 or 3.12):
   ```bash
   python -m venv .venv
   .venv/Scripts/activate            # Windows; on Linux/macOS: source .venv/bin/activate
   pip install -r requirements.txt
   ```
2. **Postgres**, for example in Docker:
   ```bash
   docker run -d --name arth-pg -e POSTGRES_PASSWORD=postgres -p 55432:5432 postgres:16
   ```
3. **Settings:** copy `.env.example` to `.env`, then set:
   - `ASI1_API_KEY` from [asi1.ai](https://asi1.ai).
   - `DATABASE_URL`, for example `postgresql://postgres:postgres@localhost:55432/postgres`.
   - `CLOUDINARY_URL`, for invoice PDF links.
   - One unique `SEED_*` per agent, for example from `python -c "import secrets; print(secrets.token_hex(32))"`. Never commit them.
4. **Create the demo shop:**
   ```bash
   python -m core.db reset
   ```
5. **Start all ten agents:**
   ```bash
   honcho start
   ```
6. **Connect to ASI:One:** open the inspector link from the orchestrator's startup log and choose **Connect → Mailbox**. Then chat with the agent in ASI:One.
7. **Testnet FET for restocks:** fund the procurement wallet (logged at startup) and the paying ASI:One wallet from the Dorado faucet. Rupees convert at `DEMO_INR_PER_FET` (default ₹10,000 = 1 FET).

## Deploy

The `Dockerfile` runs all ten agents in one container. On its first start it creates the database and the demo shop by itself (`python -m core.db init`). The agents only make outgoing connections, and the orchestrator is reached through its Agentverse Mailbox, so no public URL or open port is needed.

On Railway, for example:
1. Deploy this repo.
2. Add a Postgres database.
3. Paste the `.env` settings as variables, with `DATABASE_URL=${{Postgres.DATABASE_URL}}`.

Run only one copy at a time: two copies with the same seeds would split the orchestrator's mailbox.

## Tests

```bash
python -m pytest                                   # unit and flow tests; needs TEST_DATABASE_URL (a throwaway Postgres)
ARTH_LIVE_ASI1=1 python -m pytest tests/test_extraction_live.py   # 62 messages through live ASI1
python -m tests.demo_replay --runs 10             # the demo against live agents, real testnet payments
```

- **Unit and flow tests:** every money and tax rule, and every conversation flow, run against a fresh Postgres schema with ASI1 and the network faked.
- **Language test set:** 62 messages in English, Hindi and Hinglish, with typos and shop words like *eent*, *ret* and *saria*.

## Project layout

```
agents/
  orchestrator/   handler.py (routing, cards, payments) and agent.py (uAgent wiring, mailbox)
  ledger/ inventory/ billing/ collections/ insights/ procurement/   logic.py + agent.py each
  supplier/       one agent script, started once per config in suppliers/
core/             config, db, money (paise), gst, llm (ASI1), lang, cards, chain (FET), net, storage
db/               schema.sql, seed.sql (the demo shop)
suppliers/        shree_traders.yaml, gupta_cement.yaml, balaji_build.yaml
tests/            pytest suites, extraction_eval.py, demo_replay.py
assets/           architecture.png
docs/             Technical Design.md
```

The full design, including the data model, message flow and failure handling, is in [`docs/Technical Design.md`](docs/Technical%20Design.md).
