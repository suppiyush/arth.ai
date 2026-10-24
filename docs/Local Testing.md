# Arth: Local Testing

How to run and test every Arth flow on your own laptop, without deploying anything. All commands are for **Windows PowerShell**, run from the project folder (`Arth.Ai`).

## The one idea

The owner types what happened, in English, Hindi or Hinglish. **Arth never saves anything until the owner taps Confirm.** It always shows a card first ("here's what I'll record"), and only saves after Confirm. Questions like "how much cement is left?" get an answer straight away, with no card.

Behind the scenes, one main agent (the **orchestrator**) reads the message and asks helper agents what they would do: Ledger (khata), Inventory (stock), Billing (GST), Collections (dues). It combines their answers into that one card.

## Three ways to test

| Level | What it checks | Needs |
|---|---|---|
| 1. Automated tests (pytest) | Every money/GST rule and conversation flow, with the AI, network and blockchain faked | Docker (Postgres) |
| 2. All 10 agents on your laptop + chat script | The real agents talking to each other, exactly as deployed, minus the ASI:One app | Docker (Postgres) |
| 3. Demo replay | The full six-message demo, played and checked automatically | Docker; testnet FET only if you pay |

None of these need Agentverse, ASI:One or Supabase.

---

## First-time setup (once per laptop)

Skip this if `.venv` and `.env` already exist and the `arth-pg` container is in Docker Desktop.

1. **Install Docker Desktop** and start it. Wait for "Engine running".

2. **Create the database container** (Postgres 16 on port 55432):
   ```powershell
   docker run -d --name arth-pg -e POSTGRES_PASSWORD=postgres -p 55432:5432 postgres:16
   ```

3. **Create the Python environment.** Use Python 3.11 to 3.13; 3.14 is too new for some packages.
   ```powershell
   py -3.11 -m venv .venv
   .venv\Scripts\python -m pip install --upgrade pip
   .venv\Scripts\python -m pip install -r requirements.txt
   ```

4. **Create `.env`:** copy `.env.example` to `.env`, then set these lines:
   ```ini
   ASI1_API_KEY=
   ASI1_ENABLED=false
   DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:55432/postgres
   TEST_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:55432/postgres
   ```
   Use `127.0.0.1`, **not** `localhost`. On Windows, `localhost` can hang for a long time.

   Then give every `SEED_*` line its own random value. Run this once per seed and paste each result:
   ```powershell
   .venv\Scripts\python -c "import secrets; print(secrets.token_hex(32))"
   ```
   `.env` is ignored by git; never commit it. These local seeds give different agent addresses from the deployed ones, so **don't deploy with this laptop's `.env`**.

---

## Every time: prepare a PowerShell window

Run this in **every** new PowerShell window before any other command:

```powershell
docker start arth-pg
$env:PATH = "$PWD\.venv\Scripts;$env:PATH"
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUNBUFFERED = "1"
```

The `docker start` line is harmless if the container is already running.

---

## Level 1: Automated tests

```powershell
python -m pytest -q
```

**Expect:** `162 passed, 1 skipped` in about 20–30 seconds.

- The 1 skip is the live AI test (below); it's opt-in.
- If you see many **skipped** instead of passed, `TEST_DATABASE_URL` isn't set in `.env`.
- Each test creates its own throwaway schema in the database, so your demo data is never touched.

**Live AI test** (optional): checks that ASI1 understands 40 real messages. It needs `ASI1_API_KEY` in `.env` and uses 40 ASI1 requests.
```powershell
$env:ARTH_LIVE_ASI1 = "1"
python -m pytest tests/test_extraction_live.py
Remove-Item Env:ARTH_LIVE_ASI1
```

---

## Level 2: Run all agents and chat with Arth

You need **three PowerShell windows**, each prepared as above.

### Window 1: reset the demo shop

Run this before each practice run; it puts the shop back to the starting state.

```powershell
python -m core.db reset
```

**Expect:**
```
schema and seed applied
...
Ramesh Kumar          customer      5,400.00
Sunita Devi           customer      2,100.00
Verma Constructions   customer     36,000.00
Balaji Build          supplier          0.00
Gupta Cement          supplier          0.00
Shree Traders         supplier    -22,000.00
```

The demo shop is *Sharma Building Supplies*, with 12 items, 15 parties and 60 days of history. Cement starts at 12 bags, below its reorder level of 20.

### Window 2: start the 10 agents (leave it running)

```powershell
honcho start
```

**Expect:** about 10 lines like `Starting agent with address: agent1q...` within a few seconds.

Yellow WARNINGs are **normal on a laptop**:
- "not enough funds to register on Almanac contract"
- "Agent mailbox not found"
- "procurement wallet below 1 FET"

Stop the agents with **Ctrl+C**.

### Window 3: talk to Arth

`tests.chat_client` plays the part of ASI:One. Give it messages in order. Special words click buttons on the last card:

| Word | Does |
|---|---|
| `"@confirm"` | Clicks Confirm / Approve on the last card |
| `"@cancel"` | Clicks Cancel |
| `"@choose:1"` | Picks the 1st option in a carousel (supplier quotes) |
| `"@pay"` | Approves the FET payment; spends real **testnet** FET from this client's wallet |
| `"@reject"` | Declines the FET payment |

> **Always put quotes around the `@` words** (`"@confirm"`, not `@confirm`). PowerShell breaks them without the quotes.

Ignore the `INFO` lines in the output. Arth's replies are the indented lines after each `>`.

---

## The demo flows

Run these in Window 3, in this order, right after a reset. The expected replies below are real output from a local run with the AI turned off.

### 1. Help

```powershell
python -m tests.chat_client "help"
```

**Expect:** "Namaste! Arth is set up for Sharma Building Supplies." plus five example messages. The first sender takes over the demo shop; any other sender gets a private copy of it, with the extra line "It's a demo shop with two months of sample records, just for you."

### 2. A sale, half paid in cash

```powershell
python -m tests.chat_client "Ramesh ko 10 bag cement becha 4200 mein, aadha cash mila" "@confirm"
```

**Expect:** first a review card:
```
Sale to Ramesh Kumar
Items: 10 bag Cement
Total: ₹4,200
Paid now: ₹2,100 cash
Baaki (this entry): ₹2,100
Ramesh Kumar ka total baaki: ₹7,500
Stock: Cement: 12 → 2 bag
```
then after Confirm:
```
✅ Ho gaya!
Khata updated. Ramesh Kumar ka total baaki: ₹7,500.
Stock updated: Cement 2 bag bacha.
Ramesh Kumar ko <date a week from today> ko yaad dilaunga.
⚠ Cement kam hai: sirf 2 bag bacha (reorder level 20).
```

**For the judges:** "The AI understood *aadha cash*; the rupee amounts are calculated by code, not the AI. Nothing was saved until I confirmed."

### 3. GST bill, edited, then issued

```powershell
python -m tests.chat_client "Bill bana do Verma Constructions ke liye, 200 kg TMT 12mm" "5% discount" "@confirm"
```

**Expect:**
1. A draft bill: 200 kg TMT Bar 12mm @ ₹68 = ₹13,600, **IGST** ₹2,074.58. It's IGST because Verma is in another state.
2. After `5% discount`, the draft updates to ₹12,920.
3. After Confirm: `Bill ARTH/26-27/0001 ban gaya.`

**For the judges:** "Proper GST: inter-state sales get IGST, same-state sales get CGST + SGST, and invoice numbers run per financial year."

### 4. Who owes money

```powershell
python -m tests.chat_client "Kis kis ka udhaar baaki hai?"
```

**Expect:**
```
Udhaar list
Total udhaar ₹58,520, 3 customers se.
Verma Constructions | ₹48,920 | 26 din
Ramesh Kumar | ₹7,500 | 22 din
Sunita Devi | ₹2,100 | 9 din
Aapko suppliers ko dena hai: ₹22,000 (Shree Traders).
```

In ASI:One, each row has a **Remind** button. It produces a reminder message with a UPI payment link, then a WhatsApp link to send it. This chat script can't click Remind; to test the reminder here, turn the AI on (see below) and use:
```powershell
python -m tests.chat_client "Sunita ko payment reminder bhejo" "@confirm"
```

### 5. Stock check

```powershell
python -m tests.chat_client "Cement kitna bacha hai?"
```

**Expect:** `Cement: 2 bag bacha hai. ⚠ Kam hai (reorder level 20).`

### 6. Restock from supplier agents

Practice version (declines the payment, so no money moves):
```powershell
python -m tests.chat_client "50 bag cement mangwa do" "@choose:1" "@confirm" "@reject"
```

**Expect:**
1. Quotes from 3 supplier agents after one round of haggling:
   ```
   1. Gupta Cement [Best value, Top rated]: ₹377.30/bag · 3 din · Total ₹18,865 · mol-bhav: ₹385 → ₹377.30
   2. Shree Traders: ₹392/bag · 2 din · Total ₹19,600 · counter-offer nahi maana
   3. Balaji Build [Fastest]: ₹398/bag · 1 din · Total ₹19,900
   ```
2. After choosing: an order card with the Arth fee (1%): `You pay: ₹19,053.65 = 1.9054 FET (testnet)`.
3. After approving: a request to approve the FET payment.
4. After `@reject`: `Payment cancel hua, to order bhi cancel kar diya.`

Paid version: replace `"@reject"` with `"@pay"`. This needs testnet FET in **both** the chat client's wallet and the procurement wallet (address printed when the agents start); get it from the Dorado faucet. The payment and the supplier's on-chain check can take a minute or two.

**For the judges:** "Supplier agents compete on price, Arth haggles once, the owner pays Arth in FET, and Arth pays the supplier on the Fetch blockchain. Arth earns a 1% fee."

### 7. Profit this month

```powershell
python -m tests.chat_client "Is mahine kitna profit hua?"
```

**Expect:** sales, gross profit, expenses, net profit, top-selling items, and buttons for **Remind Verma Constructions** and **Restock Cement**. With the demo data, net profit for the month is negative.

### 8. Day book, a customer's khata, and undo

```powershell
python -m tests.chat_client "aaj ka hisaab" "Ramesh ka khata dikhao"
```

**Expect:** today's entries with cash in hand (`Galle mein cash ₹...`), then Ramesh's last few entries and his balance.

Undo the last entry:
```powershell
python -m tests.chat_client "Ramesh ko 2 bag cement becha 840 mein, cash" "@confirm" "undo" "@confirm" "Cement kitna bacha hai?"
```

**Expect:** the sale takes cement from 2 → 0. Undo shows what it will remove, then `↩ Undo ho gaya` and `Stock wapas: Cement 2 bag`. The final stock check says 2 bags again.

---

## Level 3: Replay the whole demo automatically

Stop `honcho` in Window 2 first; this script starts the agents itself.

```powershell
python -m tests.demo_replay --no-pay --runs 3
```

**Expect** (about 20 seconds per run):
```
Demo replay: 3 run(s), agents offline (ASI1 off), payment declined
run 1/3 ✓ 17s (slowest chat step 1.2s: 'Ramesh ko 10 bag cement becha 4200 mein,')
...
3/3 runs green
```
A failing run prints `✗ step '...': expected ...` with the reply it got instead, and says where the agent logs were saved (an `arth-replay` folder in your temp directory).

- `--no-pay` declines the payment, so no testnet FET is needed. Drop it to really pay.
- `--asi1` uses the real AI (about 10 requests per run).
- ⚠ It **wipes the database in `DATABASE_URL`** before each run. It refuses a remote database unless you pass `--allow-remote-reset`, but keep `DATABASE_URL` pointed at the local one anyway.

---

## AI on or off

With `ASI1_ENABLED=false` (the default above), **no LLM is used**. A simple keyword matcher in `core/llm.py` (`fallback_extract`) understands messages phrased like the demo ones above, and nothing else. Reminder wording and the profit summary use fixed templates.

To test with the real AI (ASI1), as the deployed version runs:

1. Get an API key from [asi1.ai](https://asi1.ai).
2. In `.env`, set:
   ```ini
   ASI1_API_KEY=<your key>
   ASI1_ENABLED=true
   ```
3. Restart the agents: **Ctrl+C** in Window 2, then `honcho start`.

Then try rephrasing, typos and Hindi, the way a judge might. Each message uses one ASI1 request.

Even with the AI on, it only reads the message and writes wording; every rupee, tax and total is computed in code.

---

## When something goes wrong

| Problem | Fix |
|---|---|
| A command hangs and never finishes | Docker Desktop has stopped. Start it, then `docker start arth-pg` |
| `failed to connect to the docker API` | Same: start Docker Desktop |
| pytest or the database is very slow | `.env` uses `localhost`; change it to `127.0.0.1` |
| `ModuleNotFoundError` | The window wasn't prepared; run the four "prepare" lines |
| `address already in use` / port 8001–8010 busy | Agents are already running in another window; stop them with Ctrl+C |
| "Abhi samajh nahi paa raha…" (I can't reach my language service) | The AI is off and the keyword matcher didn't recognise the phrasing. Use the demo wording, or turn the AI on |
| "Abhi confirm karne ke liye kuch nahi hai" | There was no card waiting; the previous message didn't produce one |
| "… abhi jawab nahi de raha" (agent not responding) | An agent crashed or is still starting; check Window 2 for a red error |
| Numbers differ from this guide | You didn't reset first. Run `python -m core.db reset` |

## What local testing can't show

- The ASI:One app itself: real card buttons, the Remind button and *Approve FET Payment*.
- The mailbox connection between ASI:One and the deployed orchestrator.

Do one full run on the deployed version before going on stage.
