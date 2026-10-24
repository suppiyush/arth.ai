# Arth: an AI accountant for Indian small shops

![tag:innovationlab](https://img.shields.io/badge/innovationlab-3D8BD3)
![tag:hackathon](https://img.shields.io/badge/hackathon-5F43F1)

Arth keeps the books for a kirana or building-materials shop in plain chat on **ASI:One**, in English, Hindi or Hinglish. The owner says what happened at the counter; Arth records the khata entry, updates stock, issues GST bills, chases dues with UPI reminders, answers "how is business?" from the shop's real numbers, and restocks from supplier agents, paying them on the Fetch.ai testnet. **Nothing is written until the owner confirms it on a card.**

Built for **Fetch-A-Thon 3.0** with Fetch.ai uAgents, the ASI1 model, ASI:One Interactive Cards and the Agent Payment Protocol.

> Demo video: *coming soon*

## Try saying

| Message | What happens |
|---|---|
| `Ramesh ko 10 bag cement becha 4200 mein, aadha cash mila` | Review card → sale on Ramesh's khata, ₹2,100 cash in, stock down, low-stock warning |
| `Bill bana do Verma Constructions ke liye, 200 kg TMT 12mm` | Draft GST invoice (IGST, since Verma is in Haryana); type `5% discount` to edit, then issue `ARTH/26-27/0001` |
| `Kis kis ka udhaar baaki hai?` | Dues table with **Remind** buttons → reminder with UPI link, QR code and a WhatsApp share link |
| `Cement kitna bacha hai?` | `Cement: 2 bag bacha hai. ⚠ Kam hai (reorder level 20).` |
| `50 bag cement mangwa do` | Three supplier agents quote, Arth haggles one round, carousel to choose, owner approves and pays in FET, Arth pays the supplier |
| `Is mahine kitna profit hua?` | Sales, gross and net profit, top items, dues, with Remind / Restock suggestions |
| `aaj ka hisaab`, `Ramesh ka khata dikhao`, `undo` | Day book with cash in hand, party statement, undo of the last entry |
| `Who do I owe maximum amount?`, `Ramesh se kitna lena hai?`, `15 din se purana udhaar` | Payables ranked, one party's balance and phone, aged dues |
| `Cement ka rate kya hai?`, `stock mein kitne ka maal hai` | Rate, cost, margin and GST of an item; rate list with stock value |
| `2 bag cement kharab ho gaya`, `sand asal mein 360 cft hai` | Stock adjustment card (damaged / found / physical count), no money moved |
| `Cement ka price 430 kar do`, `naya item Tile Adhesive 450, cost 380` | Edit or add an item; `Naya customer Mohan Lal 98xxxxxxxx` adds a party. All undoable |
| `Last bill dikhao`, `Verma ke baaki bills`, `Bill 3 cancel karo` | Bill in full or a filtered list; cancel any older bill (reverses its sale and stock) |
| `Bill 1 ka PDF bhejo` / `Send the PDF of the last bill` | A download link to the GST tax-invoice PDF (hosted on Cloudinary). Every issued bill gets its link automatically |
| `Ramesh ki aaj wali sale ka bill dikhao`, `aaj ki walk-in sale ka bill`, `aaj ke bills ke PDF bhejo` | The bill of an earlier sale with its PDF link; if that sale was a quick khata entry with no bill, Arth offers to make one from it (same items and amount, no second sale) |
| `Sabse zyada kya bikta hai?`, `Best customer?`, `Pichle mahine se sale badhi?`, `GST kitna?`, `Kharcha kahan gaya?`, `Cement ka sasta supplier?` | Insights reports: top items, top customers, period comparison, output GST, expenses by category, supplier rates |
| `Is mahine ka average bill?`, `Hafte ke kis din sabse zyada bikri?`, `Ramesh ne ab tak kitna cement liya?` | Anything else about the shop's records: ASI1 writes a checked, read-only SQL query and the figures come from Postgres |

## How it works

```
ASI:One user ──chat──▶ Arth orchestrator (mailbox, Chat + Payment Protocol)
                          │  ASI1: message → validated intent and fields
                          │  plan → review card → confirm → commit under one action id
          ┌───────────────┼───────────────┬──────────────┬──────────────┬─────────────┐
       Ledger        Inventory         Billing       Collections     Insights     Procurement
       (khata)       (stock)           (GST)         (dues, UPI)     (reports)        │
          └───────────────┴──── shared Postgres ─────┴──────────────┘                 │
                                                                    RFQ / Counter / PlaceOrder
                                                                  + Payment Protocol (testnet FET)
                                                                                       │
                                                 Shree Traders · Gupta Cement · Balaji Build
```

- **One door:** the owner only talks to the orchestrator. It asks the right specialists to *plan* (they never write on a plan), shows one review card, and on **Confirm** asks them to *commit* under a single action id. Commits are idempotent, so a retried confirm never counts anything twice, and **undo** reverses a whole action across agents.
- **ASI1 understands, code calculates:** ASI1 turns "aadha cash mila" into `paid_fraction: 0.5`; every rupee, tax split and total is computed in code as integer paise. For questions, ASI1 picks one of a fixed set of SQL reports and then only words its figures. A question no report covers ("average bill size?") is answered with SQL that ASI1 writes, under three guards: sqlglot lets through only a single SELECT over six read-only views; it runs as the `arth_reader` role, which can read only those views, each filtered to the asking shop, in a read-only transaction with a 3-second timeout; and code fills the figures into ASI1's answer sentence from the query result. If ASI1 is unreachable or out of quota, a keyword fallback still answers the common questions (dues, payables, balances, rates, stock, bills, reports) and simple payments and expenses; anything it can't place gets "please try again" rather than a guess.
- **GST done properly:** prices include GST by default; CGST+SGST within the state, IGST across states; invoice numbers run per financial year (April–March) and a cancelled bill keeps its number.
- **Restock with two payment legs:** suppliers quote, the two cheapest get a 2%-lower counter-offer, and the owner picks from a comparison carousel. The owner then pays Arth (order + 1% Arth fee) via ASI:One's *Approve FET Payment*, and Arth checks the transfer on-chain. Procurement then pays the supplier, which checks it on-chain and confirms the order. If a supplier can't complete, the next-best is used; if none can, the owner is refunded.

## Agents

| Agent | Role | Address |
|---|---|---|
| **Arth** (orchestrator) | The one ASI:One talks to: chat, cards, owner payments | `agent1qvch7u0jna59nw4jkpc8kw3ljjclausz3lrqxn9f8xtuqkryvws9gfsln74` |
| Arth Ledger | Khata: sales, purchases, expenses, payments, statements, day book | `agent1qgnrg6kx90cgzj2reun4e95aqsa40phjcesag8scm4q8qwt89s0w57v7ktq` |
| Arth Inventory | Stock moves, oversell checks, low-stock alerts | `agent1qwjlvwtjrfkhpk6e9vaqnyqkwr3q7zh290lnhq3e2lw6drhcg807y9yk4ju` |
| Arth Billing | GST invoices and invoice numbering | `agent1qtqangnsj4fh64wl76ex8sw6j6xj945e382rn7msq6eygx4th6y3umq2gt8` |
| Arth Collections | Dues, UPI reminders, receipts against invoices | `agent1qg8pl7pwcdwfcnx20wrlymqddy9clp5nfx6sjk6cpqdyspkreqy9cqsysfx` |
| Arth Procurement | Quotes, negotiation, paying suppliers | `agent1qtjmfuchch0apz8ntw07ld8ymhe56yjylwnw6a8hmypgmhje4scng69cqzs` |
| Arth Insights | Sales, profit and dues explained from SQL figures | `agent1qtzvj6f3qk9f25qwzte7rk8n8yazdxs9255u5kar4tuft62hhne5gwz9zq2` |
| Shree Traders | Supplier agent (independent seller) | `agent1qwyddd9xqchvp5ld5r5jmaczpqds72af9d3sp5hgm5yj2k5fx53g7sn5m4k` |
| Gupta Cement | Supplier agent (independent seller) | `agent1q0yxyx9nwyc5fvc56zq8l5dcmldmwulrp793f92pk4cxqdtemhex78kthvw` |
| Balaji Build | Supplier agent (independent seller) | `agent1qvndavra4pkeja24dhxdaj8n6v6qplk79f55gs79d2gusf9yq4yewp4aq5u` |

Addresses come from the seeds in `.env`; new seeds give new addresses.

## Tech stack

Python · uAgents 0.25.5 / uagents-core 0.4.9 (Chat Protocol 0.3.0, Payment Protocol) · ASI1 (`asi1` via the OpenAI client) · ASI:One Interactive Cards · Postgres (Supabase in production) with psycopg 3 · cosmpy for testnet FET transfers and on-chain checks · rapidfuzz for name matching · qrcode for UPI QR codes · pytest.

## Run it locally

1. **Python and dependencies** (developed on Python 3.12):
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
   - `DATABASE_URL`, e.g. `postgresql://postgres:postgres@localhost:55432/postgres`.
   - One unique `SEED_*` per agent, e.g. `python -c "import secrets; print(secrets.token_hex(32))"`. Never commit them.
4. **Create the demo shop** (Sharma Building Supplies: 12 items, 15 parties, 30 days of history):
   ```bash
   python -m core.db reset
   ```
5. **Start all ten agents:**
   ```bash
   honcho start
   ```
6. **Connect to ASI:One:** open the inspector link from the orchestrator's startup log, choose **Connect → Mailbox**, then chat with the agent in ASI:One. The first person to message it takes over the demo shop; everyone after that (judges included) gets a private copy of it, with the same 30 days of history.
7. **Testnet FET for restocks:** fund the procurement wallet (logged at startup) and the paying ASI:One wallet from the Dorado faucet. Rupees convert at `DEMO_INR_PER_FET` (default ₹10,000 = 1 FET).

## Tests

```bash
python -m pytest                                   # unit and flow tests, needs TEST_DATABASE_URL (a throwaway Postgres)
ARTH_LIVE_ASI1=1 python -m pytest tests/test_extraction_live.py   # the 62-message ASI1 extraction set (uses 62 requests)
python -m tests.demo_replay --runs 10             # full six-message demo against live agents, real testnet payments
```

- **Unit and flow tests:** every money and tax rule, plus each conversation flow, runs against a fresh Postgres schema with ASI1 and the network faked.
- **Extraction set:** 62 messages in English, Hindi and Hinglish, with typos and local words like *eent*, *ret* and *saria*. The target is 90% exact match. The first 40 scored 40/40; the 22 added for payables, balances, items, bills and reports still need a live run.
- **ASI1 quota:** the key allows about 100 requests per window (429 `rate_limit_exceeded` after that). Each chat message uses one request, plus one more for an Insights answer or a reminder's wording. When the quota runs out, Arth keeps working on the keyword fallback; the orchestrator log shows `ASI1 unavailable, using fallback` when that happens.
- **`demo_replay`:**
  - It starts the agents, resets the demo shop before each run, plays the six demo messages and their card clicks, and checks every reply.
  - By default it runs ASI1-off to save quota; pass `--asi1` for a real run, or `--no-pay` to decline the payment.
  - It **wipes `DATABASE_URL`**, so it only runs against a local database unless you pass `--allow-remote-reset`.

## Project layout

```
agents/
  orchestrator/   handler.py (routing, cards, payments) and agent.py (uAgent wiring, mailbox)
  ledger/ inventory/ billing/ collections/ insights/ procurement/   logic.py + agent.py each
  supplier/       one agent script, started once per config in suppliers/
core/             config, db, money (paise), gst, llm (ASI1), cards, chain (FET), net, specialist wiring
db/               schema.sql, seed.sql (the demo shop)
suppliers/        shree_traders.yaml, gupta_cement.yaml, balaji_build.yaml
tests/            pytest suites, extraction_eval.py, demo_replay.py, chat_client.py
docs/             Technical Design.md
```

The full design, including the data model, routing table and failure handling, is in [`docs/Technical Design.md`](docs/Technical%20Design.md).
