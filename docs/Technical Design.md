# Arth — Technical Design

Oct 21, 2026 · @Piyush Agarwal

Arth runs as ten Python uAgent processes (seven Arth agents and three supplier agents) on one VM, sharing a Supabase Postgres database, with ASI1 for language understanding and the Fetch.ai Payment Protocol on testnet FET for money movement.

## Tech stack

Everything is Python so one team can work across all agents; versions are pinned to the Innovation Lab's tested combination.

| Layer | Choice | Version | Why |
| --- | --- | --- | --- |
| Language | Python | 3.11 | Supported by uAgents; one language for all agents |
| Agent framework | uAgents + uagents-core | 0.25.5 / 0.4.9 | Pinned in the Innovation Lab examples; Chat and Payment Protocols ship in `uagents_core.contrib.protocols` |
| Chat | Agent Chat Protocol | AgentChatProtocol 0.3.0 | Required for ASI:One discovery |
| UI | ASI:One Interactive Cards (`MetadataContent`) | card\_protocol\_version 1 | Confirm, compare and invoice cards inside the chat, no frontend |
| LLM | ASI1 API, model `asi1`, via the `openai` client | latest | Judges score ASI1 as core reasoning; OpenAI-compatible endpoint |
| Validation | Pydantic | 2.x | Validate every LLM extraction before it touches the database |
| Database | Supabase Postgres + `psycopg` | psycopg 3.2 | Free tier, SQL for reports, storage bucket for invoice PDFs |
| Payments | Payment Protocol, `fet_direct` on stable testnet (`atestfet`) + `cosmpy` | latest | Native ASI:One approve flow; on-chain verification |
| Customer payment links | UPI intent links + QR (`qrcode` lib) | 7.x | Works with any UPI app; no gateway onboarding needed |
| Invoices | Jinja2 HTML → WeasyPrint PDF | 62.x | One template for card text and PDF |
| Process manager | honcho (Procfile) or Docker Compose | latest | Starts all 10 agents with one command |
| Hosting | One small VM (2 vCPU, 4 GB), Ubuntu 24.04 | — | Mailbox agents must stay online for judges after the event |
| WhatsApp (P2) | Twilio WhatsApp sandbox + FastAPI | FastAPI 0.115 | Fastest sandbox to set up |
| Voice (P2) | Sarvam AI speech-to-text | API | Strong on Hindi and Hinglish |
| Tests | pytest + a scripted chat client | 8.x | Replays the demo end to end before every recording |

**Not used:** a web frontend, LangChain or CrewAI (no benefit at this size), Stripe (India onboarding is slow; FET testnet covers the Payment Protocol).

## Architecture

&#91;embedded content: Arth architecture · 10 agents, 3 shared services\]

The owner only ever talks to the orchestrator; specialists exchange typed messages with it, write to one shared Postgres, and only procurement talks to the external supplier agents and pays them on the Fetch testnet.

## Repository structure

One repo, one shared `core` package, one folder per agent; every agent imports `core` and never touches another agent's tables directly.

```text
arth/
├── core/                     # shared library, no agent code
│   ├── config.py             # env loading, network, seeds
│   ├── db.py                 # psycopg pool, transaction helper
│   ├── models.py             # pydantic message models (section: Agent messaging)
│   ├── llm.py                # ASI1 client, extract(), explain()
│   ├── cards.py              # review/carousel/detail/custom card builders
│   ├── money.py              # paise <-> ₹ formatting, Indian grouping
│   ├── gst.py                # tax split, invoice numbering
│   ├── fuzzy.py              # party/item name matching (rapidfuzz)
│   └── fet.py                # send_tokens, verify_payment (cosmpy)
├── agents/
│   ├── orchestrator/agent.py # Chat Protocol + Payment Protocol (seller)
│   ├── ledger/agent.py
│   ├── inventory/agent.py
│   ├── billing/agent.py
│   ├── collections/agent.py
│   ├── procurement/agent.py  # Payment Protocol (buyer)
│   ├── insights/agent.py
│   └── supplier/agent.py     # run 3 times with different configs
├── suppliers/                # shree_traders.yaml, gupta_cement.yaml, balaji_build.yaml
├── bridge/whatsapp.py        # P2: FastAPI + Twilio webhook
├── db/schema.sql
├── db/seed.sql               # demo shop: 12 items, 15 parties, 60 days of history
├── templates/invoice.html
├── tests/                    # unit tests + demo_replay.py
├── Procfile
├── requirements.txt
├── .env.example
└── README.md                 # agent names, addresses, badges, demo link
```

## Data model

Ten tables cover every P0 requirement; money is stored as integer paise and every row written by an agent carries the `action_id` that created it, which is what makes undo and idempotency work.

```sql
create table business (
  id uuid primary key default gen_random_uuid(),
  owner_address text unique not null,      -- ASI:One sender address (G-10)
  name text not null, gstin text, state_code char(2) default '08',
  upi_vpa text, fet_wallet text, created_at timestamptz default now());

create table party (
  id uuid primary key default gen_random_uuid(),
  business_id uuid references business, kind text check (kind in ('customer','supplier')),
  name text not null, phone text, state_code char(2),
  agent_address text,                       -- set for supplier agents
  opening_balance_paise bigint default 0, unique (business_id, name));

create table item (
  id uuid primary key default gen_random_uuid(),
  business_id uuid references business, name text not null, unit text,
  hsn text, gst_rate numeric(4,2), cost_paise bigint, price_paise bigint,
  stock_qty numeric(12,3) default 0, reorder_level numeric(12,3) default 0,
  unique (business_id, name));

create table action (                      -- one row per confirmed user action
  id uuid primary key, business_id uuid references business,
  kind text, payload jsonb, status text default 'committed', -- committed | undone
  source_msg_id uuid unique,               -- idempotency (G-7)
  created_at timestamptz default now());

create table txn (                         -- the ledger (khata)
  id bigserial primary key, business_id uuid references business,
  action_id uuid references action, party_id uuid references party,
  kind text check (kind in ('sale','purchase','expense','receipt','payment','credit_note')),
  amount_paise bigint not null, mode text check (mode in ('cash','upi','bank','credit')),
  category text, invoice_id uuid, note text, txn_date date default current_date);

create table stock_move (
  id bigserial primary key, action_id uuid references action, item_id uuid references item,
  qty numeric(12,3) not null, reason text);   -- + in, - out

create table invoice (
  id uuid primary key default gen_random_uuid(), business_id uuid references business,
  action_id uuid references action, number text unique, party_id uuid references party,
  status text default 'draft',              -- draft | issued | paid | partly_paid | cancelled
  taxable_paise bigint, cgst_paise bigint, sgst_paise bigint, igst_paise bigint,
  total_paise bigint, discount_pct numeric(5,2) default 0, pdf_url text, issued_at timestamptz);

create table invoice_line (
  invoice_id uuid references invoice, item_id uuid references item,
  qty numeric(12,3), rate_paise bigint, gst_rate numeric(4,2), amount_paise bigint);

create table purchase_order (
  id uuid primary key default gen_random_uuid(), business_id uuid references business,
  action_id uuid references action, item_id uuid references item, qty numeric(12,3),
  supplier_id uuid references party, quotes jsonb, unit_price_paise bigint, total_paise bigint,
  arth_fee_paise bigint, user_tx text, supplier_tx text,
  status text default 'quoting');            -- quoting | awaiting_approval | awaiting_user_payment | paying_supplier | confirmed | failed | cancelled

create table reminder (
  id bigserial primary key, party_id uuid references party,
  amount_paise bigint, message text, upi_link text, follow_up date, sent_at timestamptz);

create view party_balance as         -- positive = they owe us
select p.id, p.business_id, p.name, p.kind,
  p.opening_balance_paise + coalesce(sum(case
    when t.kind in ('sale','payment') then t.amount_paise
    when t.kind in ('receipt','purchase','credit_note') then -t.amount_paise end), 0) as balance_paise
from party p left join txn t on t.party_id = p.id
  and t.action_id not in (select id from action where status = 'undone')
group by p.id;
```

A sale with partial payment writes two `txn` rows (sale ₹4,200 credit and receipt ₹2,100 cash) under one `action_id`; the balance view then shows ₹2,100 due without a separate dues table.

## Agent messaging

Specialists speak a small typed protocol with two phases, plan then commit, so the orchestrator can show a confirm card before anything is written and can undo by action ID afterwards.

```python
# core/models.py
from uagents import Model

class Plan(Model):            # orchestrator -> specialist: "what would you do?"
    request_id: str
    business_id: str
    intent: str               # e.g. "sale", "bill", "stock_query"
    fields: dict              # validated extraction from ASI1

class PlanResult(Model):      # specialist -> orchestrator
    request_id: str
    ok: bool
    summary_rows: list[dict]  # [{"label": "Total", "value": "₹4,200"}] for the card
    effects: dict             # exact rows to write, replayed on commit
    question: str | None = None   # G-3: ask instead of guessing
    error: str | None = None

class Commit(Model):          # orchestrator -> specialist after Confirm
    request_id: str
    action_id: str
    effects: dict

class Committed(Model):
    request_id: str
    ok: bool
    message: str
    follow_ups: list[dict] = []   # e.g. {"type": "low_stock", "item_id": ...}

class Undo(Model):
    action_id: str

class Query(Model):           # read-only requests (balances, stock, dues, reports)
    request_id: str
    business_id: str
    intent: str
    fields: dict

class QueryResult(Model):
    request_id: str
    ok: bool
    card_kind: str            # which card the orchestrator should render
    card_payload: dict
    text: str
```

**Request/response pattern.** The orchestrator keeps a dict of `asyncio.Future`s keyed by `request_id`; each specialist reply resolves its future. A 10-second `asyncio.wait_for` on each future gives the G-9 timeout and fallback reply. (uAgents also offers `ctx.send_and_receive`; check it in 0.25.5 and use it if it behaves, the futures map is the fallback.)

**Multi-agent commits.** A confirmed sale is one `action_id` sent as three `Commit`s: Ledger writes the `txn` rows, Inventory the `stock_move`, Collections sets a follow-up date. Each specialist ignores an `action_id` it has already applied, so a retry never double-posts.

**Undo.** The orchestrator marks the action `undone` and broadcasts `Undo(action_id)`; Ledger needs nothing more (the balance view excludes undone actions), Inventory writes reversing `stock_move` rows, Billing marks the invoice cancelled.

## Orchestrator implementation

The orchestrator is the only agent ASI:One talks to: it turns a message into a validated intent with ASI1, asks the right specialists for a plan, shows a card, and commits on Confirm.

**1. Intent extraction (core/llm.py).** ASI1 is called through the OpenAI client at `https://api.asi1.ai/v1` with model `asi1`. The system prompt lists the 11 intents and the JSON shape, includes 3 Hinglish examples, and passes the shop's item and party names so ASI1 can resolve them. The reply is parsed with Pydantic; on a parse failure the model gets one repair attempt with the validation error, then the user gets a clarifying question.

```python
class Extraction(BaseModel):
    intent: Literal["sale","purchase","expense","payment_in","payment_out",
                    "stock_query","bill","dues","restock","question","undo","help"]
    party: str | None = None
    lines: list[Line] = []            # Line(item, qty, unit, rate)
    amount: float | None = None       # rupees as spoken; converted to paise in code
    paid: float | None = None
    paid_fraction: float | None = None   # "half paid" -> 0.5
    mode: Literal["cash","upi","bank","credit"] | None = None
    discount_pct: float | None = None
    period: str | None = None         # "this_month", "today", "2026-10"
    missing: list[str] = []           # fields the model could not fill
```

Arithmetic never comes from the LLM: "half paid" is extracted as `paid = amount / 2` by code from a `paid_fraction` hint, and totals are recomputed from lines.

**2. Routing table.**

| Intent | Plan from | Commit to | Card |
| --- | --- | --- | --- |
| sale | Ledger + Inventory | Ledger, Inventory, Collections | review |
| purchase / expense / payment\_in / payment\_out | Ledger (+ Inventory for purchase) | same | review |
| bill | Billing | Billing → Ledger, Inventory, Collections | detail (invoice) |
| stock\_query | Inventory (Query) | — | custom table |
| dues | Collections (Query) | — | custom list with Remind buttons |
| restock | Procurement | Procurement | carousel (quotes) → review → payment |
| question | Insights (Query) | — | detail (summary + action buttons) |
| undo | — | broadcast Undo | text |
| help | — | — | custom (5 sample commands) |

**3. Session state.** `ctx.storage[sender]` holds `{business_id, pending: {request_id, intent, effects, agents}, last_action_id, po_id}`. A pending plan expires after 10 minutes.

**4. Card selections.** Every CTA carries `{"action": ..., "request_id": ...}`. The handler tries `json.loads` first (direct @mention), then falls back to keyword matching on prose (planner-mediated: "confirm", "cancel", supplier names), and finally treats the text as a new request. Typed words like "haan", "ok", "confirm" also confirm the pending plan.

**5. Handler outline.**

```python
@chat_proto.on_message(ChatMessage)
async def on_chat(ctx, sender, msg):
    await ctx.send(sender, ChatAcknowledgement(acknowledged_msg_id=msg.msg_id))
    if seen(msg.msg_id): return                       # G-7
    text = join_text(msg)
    biz = await ensure_business(sender)               # onboarding on first message
    sel = parse_selection(text)
    if sel and (p := pending(ctx, sender)):
        return await handle_selection(ctx, sender, p, sel)
    ext = await llm.extract(text, biz)
    if ext.missing:
        return await reply_text(ctx, sender, ask_for(ext.missing))
    if ext.intent in READ_INTENTS:
        res = await ask(ctx, agent_for(ext.intent), Query(...))
        return await reply_card(ctx, sender, res.card_kind, res.card_payload, res.text)
    plans = await gather_plans(ctx, ext)              # parallel Plan to each agent
    if q := first_question(plans):
        return await reply_text(ctx, sender, q)
    save_pending(ctx, sender, ext, plans)
    await reply_card(ctx, sender, "review", review_card(plans), "Check and confirm:")
```

`core/cards.py` wraps the documented metadata keys (`card_protocol_version` "1", `requires_card_interaction`, `card_kind`, `card_payload` as a JSON string) and keeps every payload under 64 KB. Every card message also carries a plain-text fallback, since an invalid card is silently shown as text.

## Specialist agents

Each specialist is a thin uAgent around `core` functions: it answers `Plan` and `Query` without writing, and writes only on `Commit`.

**Ledger** (L-1 to L-10)

- `plan_sale`, `plan_purchase`, `plan_expense`, `plan_payment` build `txn` rows; a partial payment becomes a sale row (mode credit) plus a receipt row.
- Party resolution uses `rapidfuzz` against existing names (score ≥ 85 = match, two matches within 5 points = ask); an unknown name returns a question offering to create the party.
- Queries: party statement (last 10 rows + `party_balance`), day book, cash in hand (sum of cash `txn` by sign).

**Inventory** (I-1 to I-8)

- Plan checks stock and returns `stock_move` rows; insufficient stock returns a question with Sell anyway / Cancel (I-6).
- After every commit it compares `stock_qty` with `reorder_level` and returns a `low_stock` follow-up; the orchestrator appends a Restock button to its reply.
- `stock_qty` is updated in the same DB transaction as the `stock_move` insert.

**Billing and GST** (B-1 to B-11)

- `core/gst.py`: if the party's `state_code` equals the business's, split GST into CGST and SGST halves, else IGST; round each line to the paisa, the total to the rupee with a round-off line.
- Invoice numbers `ARTH/26-27/0001` come from a per-business, per-financial-year counter inside the commit transaction (April to March).
- The draft lives in the orchestrator's pending plan, so edits ("5% discount") re-plan without touching the DB.
- On commit, Billing writes `invoice` + `invoice_line` and returns sale effects that the orchestrator commits to Ledger, Inventory and Collections under the same `action_id`.
- P1 PDF: render `templates/invoice.html` with Jinja2, convert with WeasyPrint, upload to a Supabase Storage public bucket, store `pdf_url`.

**Collections** (C-1 to C-7)

- Dues come from `party_balance` (positive balances), aged by the oldest unpaid sale.
- Reminder text is written by ASI1 in the owner's language with amount and bill numbers filled in by code; the UPI link is `upi://pay?pa=<vpa>&pn=<shop>&am=<amount>&cu=INR&tn=<bill no>`, with a QR image (`qrcode` lib) for desktop viewers.
- The approved reminder is returned as text plus a `https://wa.me/<phone>?text=<encoded>` link; direct sending waits for the P2 bridge.
- A receipt is applied to the oldest open invoices first and updates their status.

**Insights** (Q-1 to Q-6)

- Fixed SQL queries compute the numbers (period sales, purchases, expenses, gross profit from `cost_paise`, top items, receivables vs payables for the next 7 days).
- ASI1 receives only that JSON and writes three lines of explanation plus up to two suggestions, each mapped to a button (`remind`, `restock`).
- Free-text business questions get the same context block, so answers cite the shop's real figures.

## Procurement and payments

A restock moves money twice: the owner pays Arth inside ASI:One (Arth is the seller), then Arth's procurement agent pays the winning supplier agent (Arth is the buyer). Both legs use the Payment Protocol with `fet_direct` on stable testnet and are verified on-chain.

**Supplier agents.** One `agents/supplier/agent.py`, started three times with different YAML configs (Shree Traders, Gupta Cement, Balaji Build). Each config sets per-item base price, a floor price for negotiation, stock, delivery days and a rating, so the three quotes genuinely differ. Each supplier is its own Agentverse agent with its own wallet and address.

```python
# negotiation messages (core/models.py)
class RFQ(Model):          po_id: str; item: str; qty: float; need_by: str
class Quote(Model):        po_id: str; supplier: str; unit_price_paise: int; delivery_days: int; rating: float; ok: bool
class Counter(Model):      po_id: str; target_unit_price_paise: int
class PlaceOrder(Model):   po_id: str; unit_price_paise: int; qty: float
class OrderConfirmed(Model): po_id: str; dispatch_eta: str
```

**Flow and the code behind each step**

1. *Restock request.* Procurement creates a `purchase_order` (status `quoting`) and sends `RFQ` to every supplier with `agent_address` set. Discovery starts from seeded addresses; P1 adds an Agentverse search for agents tagged `arth-supplier`.
2. *Quotes.* Replies are collected for 6 seconds; a silent supplier is skipped and noted on the card.
3. *One counter round (P-4).* The top two get `Counter(target = best price − 2%)`; each accepts if the target is above its floor, else holds its quote.
4. *Comparison card.* A carousel shows each supplier with price, delivery days, rating and total; the best value carries a badge. The Arth fee (`ARTH_FEE_PCT`, default 1%) appears as its own row.
5. *Approve.* A review card confirms supplier, quantity, total and fee in ₹ and in FET.
6. *Leg 1, owner pays Arth.* The orchestrator (Payment Protocol role `seller`) sends `RequestPayment` with `Funds(currency="FET", amount=<total+fee in FET>, payment_method="fet_direct")`, `recipient` = its own wallet, `metadata.fet_network = "stable-testnet"`. ASI:One shows Approve FET Payment. On `CommitPayment` the orchestrator reads `metadata.buyer_fet_wallet`, verifies the transfer with `cosmpy` `LedgerClient.query_tx` (recipient, sender and amount in `atestfet`), then sends `CompletePayment`; otherwise `CancelPayment` and the order stops.
7. *Leg 2, Arth pays the supplier.* Procurement sends `PlaceOrder`; the supplier (role `seller`) answers with `RequestPayment`. Procurement (role `buyer`) transfers with `LedgerClient.send_tokens` from its own wallet, then sends `CommitPayment(transaction_id=<tx hash>, metadata={"buyer_fet_wallet": <its address>})`. The supplier verifies on-chain, sends `CompletePayment` and `OrderConfirmed`.
8. *Post.* Procurement commits under one `action_id`: a purchase `txn` and payment `txn` (Ledger), a `stock_move` (Inventory), PO status `confirmed`. The chat shows the supplier tx hash as a testnet explorer link.

**Failure handling (P-9).** If leg 2 fails verification, procurement retries once, then places the order with the next-best supplier at its quoted price. If no supplier completes, procurement refunds the owner by `send_tokens` to the leg-1 `buyer_fet_wallet` and marks the PO `failed`.

**Amounts.** Testnet tokens stand in for rupees at a fixed demo rate (`DEMO_INR_PER_FET`, for example 10,000), so a ₹19,000 order costs 1.9 test FET plus the 1% fee. Fund the procurement wallet from the Dorado faucet before the demo and check its balance on startup.

**Monetisation story for judges:** the owner pays for nothing upfront; Arth earns 1% on each completed restock, collected inside the same payment the owner already approves.

## WhatsApp bridge (P2)

The bridge is a second door into the same orchestrator, so nothing about the agents changes; build it only after the full ASI:One demo passes.

1. Twilio WhatsApp sandbox posts incoming messages to `POST /whatsapp` on a FastAPI app on the same VM.
2. Voice notes (`MediaContentType0` = audio/ogg) are downloaded and sent to Sarvam AI speech-to-text; the transcript becomes the message text.
3. The bridge has its own `uagents-core` identity, registered on Agentverse like the documented card client, and sends a `ChatMessage` to the orchestrator with `send_message_to_agent`. The orchestrator maps the bridge sender plus the WhatsApp number to the same `business_id` as the owner's ASI:One address (linked once with a 6-digit code).
4. Replies come back to the bridge webhook; cards are flattened to text ("Reply 1 to confirm, 2 to cancel") and returned through the Twilio API.

The same idempotency (`msg_id`) and confirm rules apply, so a WhatsApp voice note can never post an entry without an explicit "1" or "haan".

## Configuration and deployment

All ten agents run as mailbox agents on one VM under one Procfile, so the whole system restarts with one command and stays reachable from ASI:One after the event.

```text
# .env.example
ASI1_API_KEY=
DATABASE_URL=postgresql://...supabase.co:5432/postgres
FET_USE_TESTNET=true
DEMO_INR_PER_FET=10000
ARTH_FEE_PCT=1
SEED_ORCHESTRATOR=...      # one unique seed per agent; never commit
SEED_LEDGER=...  SEED_INVENTORY=...  SEED_BILLING=...
SEED_COLLECTIONS=...  SEED_PROCUREMENT=...  SEED_INSIGHTS=...
SEED_SUPPLIER_SHREE=...  SEED_SUPPLIER_GUPTA=...  SEED_SUPPLIER_BALAJI=...
CLOUDINARY_URL=                       # invoice PDFs: cloudinary://key:secret@cloud
SARVAM_API_KEY=  TWILIO_SID=  TWILIO_TOKEN=   # P2
```

```text
# Procfile (ports 8001-8010, each agent mailbox=True)
orchestrator: python -m agents.orchestrator.agent
ledger:       python -m agents.ledger.agent
inventory:    python -m agents.inventory.agent
billing:      python -m agents.billing.agent
collections:  python -m agents.collections.agent
procurement:  python -m agents.procurement.agent
insights:     python -m agents.insights.agent
sup_shree:    python -m agents.supplier.agent suppliers/shree_traders.yaml
sup_gupta:    python -m agents.supplier.agent suppliers/gupta_cement.yaml
sup_balaji:   python -m agents.supplier.agent suppliers/balaji_build.yaml
```

**Agentverse registration, per agent**

1. Start the process; copy the inspector URL from the log; Connect → Mailbox.
2. Edit the profile: name ("Arth", "Arth Ledger", ...), handle (`@arth`), description, and a README that includes both required badges, `![tag:innovationlab](https://img.shields.io/badge/innovationlab-3D8BD3)` and `![tag:hackathon](https://img.shields.io/badge/hackathon-5F43F1)`.
3. The orchestrator README is written for ASI:One discovery: what Arth does, 5 example prompts in English and Hinglish, and the keywords "accounting", "khata", "GST billing", "inventory", "kirana", "small business India".
4. Only the orchestrator includes the Chat Protocol with `publish_manifest=True`; specialists publish their own protocols so they show up as working agents.
5. Copy every agent's address into the repo README table.

**Hosting.** Docker Compose on a 2 vCPU / 4 GB VM with `restart: always`. Agent state lives in Postgres, so a restart loses nothing; the uAgents storage files hold only session scratch.

**Latency check.** Agent-to-agent messages go through mailboxes. If one hop takes more than 2 seconds in testing, give the specialists public endpoints on the VM (behind Caddy) and keep the mailbox only on the orchestrator.

## Testing and reliability

The demo is only as good as its worst run, so every money and tax rule has a unit test and the full six-message demo is replayed automatically before each recording.

| Layer | What is tested | How |
| --- | --- | --- |
| Unit | GST split and rounding, invoice numbering across financial years, `party_balance` signs, paise formatting (₹1,25,000) | pytest against a throwaway Postgres schema |
| Extraction | 40 sample messages (English, Hindi, Hinglish, typos) → expected intent and fields | pytest calling ASI1; target ≥ 90% exact match, failures feed the prompt examples |
| Agents | Plan, Commit, Undo for each specialist; duplicate `msg_id` and duplicate `action_id` | uAgents agents started in-process with a test client |
| Payments | Leg 1 and leg 2 on testnet; verification rejects wrong amount, wrong sender, unknown tx | small real testnet transfers |
| End to end | The six demo messages including card selections | `tests/demo_replay.py`, modelled on the documented card client, sends JSON selections to the orchestrator and checks each reply |

**Reliability rules in code**

- Every external call (ASI1, DB, ledger, agents) is wrapped with one retry and a timeout; the user sees an error card with a next step, never silence (G-8).
- If ASI1 is down, a regex fallback handles the six demo patterns so the recording can still proceed.
- Seed data (`db/seed.sql`) gives the demo shop 60 days of history, so Insights has real numbers on the first try.
- A `/health` check per agent and a startup log of wallet balances catch an unfunded procurement wallet before the demo.

## Build order

Build in the order the demo needs it: one sale working end to end in ASI:One by tonight, the paid restock by Oct 23, and Oct 24 only for polish and submission. Confirm on the event Discord that building before Oct 24 is allowed.

**Oct 21: skeleton and first sale**

- [ ] Create repo, `requirements.txt`, `.env.example`, Supabase project, run `schema.sql` and `seed.sql`
- [ ] Get ASI1 API key, Agentverse account, testnet FET from the faucet for orchestrator, procurement and suppliers
- [ ] `core/` basics: config, db, money, models, llm.extract, cards
- [ ] Orchestrator with Chat Protocol on mailbox; reply to "help" from ASI:One
- [ ] Ledger and Inventory agents; sale with partial payment confirmed through a review card

**Oct 22: billing, collections, queries**

- [ ] Billing agent with GST split, numbering, draft edits, invoice card
- [ ] Collections agent: dues card, ASI1 reminder, UPI link and QR
- [ ] Statement, day book and stock queries; undo across agents
- [ ] Extraction test set (40 messages) passing at 90%

**Oct 23: procurement and payments**

- [ ] Supplier agent with three YAML configs; RFQ, quotes, one counter round
- [ ] Comparison carousel and approve review card
- [ ] Leg 1: orchestrator `RequestPayment` and on-chain verification in ASI:One
- [ ] Leg 2: procurement pays supplier, `OrderConfirmed`, purchase posted
- [ ] Insights agent with seed history; `demo_replay.py` green 10 runs in a row

**Oct 24: polish and submit**

- [ ] Deploy to the VM with Docker Compose; register and describe all agents on Agentverse
- [ ] P1 items in order if time: invoice PDF, payables, remind-all; then P2 WhatsApp bridge
- [ ] Record the ASI:One shared chat and the 3 to 5 minute video
- [ ] README with agent names, addresses, badges; Devpost submission with all links

**Cut order if late:** WhatsApp, then invoice PDF, then the counter-offer round. Never cut leg 1 or leg 2 of the payment.

## Sources

- [Fetch-A-Thon 3.0 hackpack](https://innovationlab.fetch.ai/events/hackathons/fetch-a-thon-3/hackpack): requirements, badges, judging
- [ASI:One Interactive Cards](https://innovationlab.fetch.ai/resources/docs/interactive-cards/asi-interactive-cards): metadata keys, card kinds, selection formats
- [ASI:One compatible uAgents](https://innovationlab.fetch.ai/resources/docs/examples/chat-protocol/asi-compatible-uagents): Chat Protocol 0.3.0, mailbox, ASI1 API, uagents 0.25.5
- [FET Image Agent Payment Protocol](https://innovationlab.fetch.ai/resources/docs/examples/agent-transaction/fet-image-agent-payment-protocol): `fet_direct`, `buyer_fet_wallet`, cosmpy verification, testnet faucet
- [Agent Payment Protocol](https://uagents.fetch.ai/docs/guides/agent-payment-protocol): roles and message flow
