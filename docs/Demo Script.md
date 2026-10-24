# Arth: Demo Video Script

**A day at Sharma Building Supplies**, a building-materials shop in Jaipur. The owner, Mr. Sharma, runs the whole day from chat with Arth on ASI:One: he opens the shop, serves walk-in and regular customers, makes a GST bill, takes payments, receives a supplier delivery, chases udhaar, closes the day's books, and finally restocks from supplier agents, paying in FET.

About 10–12 minutes of video. Type each message exactly as written, in order: later scenes rely on what earlier ones recorded.

**How to read this script:**
- **Type** is the message to send. **Click** is a button on Arth's card (typing `haan` or `confirm` works too).
- **Expect** is what Arth answered in a dry run on a freshly reset shop. Figures marked *(≈)* depend on the recording date, because the demo history is "the last 30 days"; everything else should match exactly. Sentences that ASI1 writes (summaries, reminders, SQL answers) vary in wording, but the figures don't.
- **Say** is a voice-over line.
- **Arth replies in the language you write in.** The script mixes Hinglish, English and one Hindi line on purpose, so the video shows all three.

---

## Before you record

1. **Reset the shop**, so every figure matches this script. This wipes all test entries:
   ```powershell
   python -m core.db reset
   ```
2. **Restart the agents** (`honcho start`, or Ctrl+C and start again). Wait for `Agent registration status updated to active` on the orchestrator line.
3. **Open a fresh chat with Arth** in ASI:One. The first message claims the shop for your ASI:One identity. If your chat ever has a new sender address, it gets a fresh private copy of the demo shop, so the story still starts from the same point.
4. **Invoice PDFs:** `CLOUDINARY_URL` must be in `.env` for Arth to send a download link; without it the PDF is only saved in the `invoices/` folder on your laptop.
5. **ASI1 quota:** the whole script uses about **45 ASI1 requests**, and the key allows 100 a day (resets 08:30 IST). Record early in the day. When the quota runs out, replies turn plain or say "samajh nahi paa raha".
6. **Testnet FET for the finale (Scene 14):** fund the **Procurement wallet** (printed when the procurement agent starts) and your **ASI:One wallet** with at least 2 testnet FET each from the Dorado faucet. Without funds, end the video at the supplier carousel.
7. Optional: keep the **"Arth agents (honcho)"** window on a second screen. In Scene 9, cut to the log line showing how Arth routed "least" to a SQL query; it shows the agents at work.

**Starting position after the reset:**
- **Customers who owe the shop:** Verma Constructions ₹36,000, Kailash Builders ₹17,000, Meena Interiors ₹7,600, Ramesh ₹5,400, Arif Electricals ₹3,500, Deepak Yadav ₹3,300 and Sunita ₹2,100.
- **Other customers:** Gopal Singh has paid in full; Arora Homes has paid a ₹5,000 advance.
- **Suppliers the shop owes:** Shree Traders ₹22,000, Jaipur Paints & Hardware ₹13,800, Rajasthan Sand Suppliers ₹9,000 and Mahaveer Hardware ₹6,800.
- **Stock:** Cement is at 12 bags against a reorder level of 20.

---

## Scene 0: Intro (voice-over, no typing)

> "Lakhs of small shops in India still keep their accounts in a paper khata. Meet Arth, an AI accountant that lives in chat. Let's spend a day at Sharma Building Supplies in Jaipur. Mr. Sharma just tells Arth what happens at the counter, in Hindi, English or Hinglish, and Arth keeps the books: sales, udhaar, GST bills, stock, and even restocking from supplier agents with payments on Fetch.ai. Nothing is written until he confirms it."

---

## Morning

### Scene 1: Opening the shop

**Type:** `Good morning! Stock dikhao`

**Expect:** "Namaste! Arth Sharma Building Supplies ke liye taiyaar hai." and a *Stock* card, subtitle "1 item kam hain: Cement", one row per item:
- **⚠ Cement**: 12 bag bacha · reorder level 20
- **Binding Wire**: 30 kg bacha · reorder level 10
- … 12 items in all

**Type:** `Cement ka rate kya hai?`

**Expect:** "Cement: bechne ka rate ₹420/bag (GST included), kharid ₹380, margin ₹40 (10%). GST 28% (HSN 2523). Stock 12 bag."

**Say:** "Before the first customer, Sharma ji checks his stock. Cement is already running low; keep an eye on that."

### Scene 2: A walk-in customer buys a few things

**Type:** `A walk-in customer bought 2 bags of wall putty and 5 kg nails, paid cash`

**Expect** "Please check and confirm:" (in English, because he typed English) and a card *Walk-in sale*:
- Items: 2 bag Wall Putty 40kg, 5 kg Nails
- Total **₹2,250** · Paid ₹2,250 cash
- Stock: Wall Putty 40kg 18 → 16 bag · Nails 25 → 20 kg

**Click** Confirm. **Expect:** "✅ Done! Khata updated. Stock now: Wall Putty 40kg 16 bag, Nails 20 kg."

**Say:** "Two products in one sentence. He didn't give prices, so Arth used the shop's rate list: 2 × ₹850 plus 5 × ₹110."

### Scene 3: A regular customer pays half, the rest on udhaar

**Type:** `Ramesh ko 10 bag cement becha 4200 mein, aadha cash mila`

**Expect** "Check karke confirm kijiye:" and a card *Ramesh Kumar ko bikri*:
- Total ₹4,200 · Abhi mila ₹2,100 cash · Is entry ka baaki ₹2,100
- Ramesh Kumar ka total baaki **₹7,500**
- Stock: Cement **12 → 2 bag**

**Click** Confirm. **Expect:** "✅ Ho gaya! Khata update ho gaya. Ramesh Kumar ka total baaki: ₹7,500.", a follow-up reminder date a week away, and **"⚠ Cement kam hai: sirf 2 bag bacha (reorder level 20)."**

**Say:** "Back in Hinglish, so Arth answers in Hinglish. 'Aadha cash' becomes ₹2,100 received and ₹2,100 on Ramesh's khata. And now cement is almost gone."

### Scene 4: A new customer, part UPI, part udhaar

**Type:** `Piyush Agarwal ko 3 bucket white paint becha, 5000 UPI se diye, baaki udhaar`

**Expect** a card *Piyush Agarwal ko bikri*:
- Customer: **Piyush Agarwal (naya)**
- Total ₹13,350 · Abhi mila ₹5,000 UPI · Is entry ka baaki **₹8,350**

**Click** Confirm. **Expect:** "Piyush Agarwal ka total baaki: ₹8,350." and "⚠ White Paint 20L kam hai: sirf 3 bucket bacha (reorder level 4)."

**Type:** `Piyush ka number 9876543210 hai`

**Expect:** a card *Piyush Agarwal badlein*, Phone — → +91 98765 43210. **Click** Confirm. ("Piyush Agarwal update ho gaya.")

**Say:** "Piyush is a new customer, so Arth opens his khata on the spot. Saving his number means reminders can go straight to his WhatsApp."

---

## Midday

### Scene 5: A contractor wants a GST bill

**Type:** `Verma Constructions ke liye bill banao, 200 kg TMT 12mm aur 100 kg TMT 8mm`

**Expect** "Bill ka draft. Kuch badalna ho to likhiye, jaise "5% discount"." and a card *Verma Constructions ka GST bill*:
- Invoice no ARTH/26-27/0001 (confirm par)
- 200 kg TMT Bar 12mm @ ₹68 = ₹13,600 · 100 kg TMT Bar 8mm @ ₹70 = ₹7,000
- **IGST ₹3,142.38** (Verma is in Haryana, so inter-state) · Bill total ₹20,600

**Type:** `5% discount`

**Expect** the draft re-priced: lines ₹12,920 and ₹6,650, taxable value ₹16,584.74, IGST ₹2,985.26, **bill total ₹19,570**.

**Click** *Confirm karke bill banao*. **Expect** one message: "✅ Ho gaya! Bill ARTH/26-27/0001 ban gaya.", Verma's total baaki **₹55,570**, TMT stock down (650 kg and 320 kg), and "📄 Bill ARTH/26-27/0001 ka PDF: (link)". Open the link to show the tax invoice: GSTIN, HSN codes, the IGST split and the amount in words. (Later, "Bill 1 ka PDF bhejo" sends the same link again.)

**Say:** "Proper GST: IGST for an out-of-state buyer, CGST plus SGST within Rajasthan, and invoice numbers that run per financial year. The bargaining happens right in the chat."

### Scene 6: An old udhaar is cleared

**Type:** `Sunita ne apna 2100 ka udhaar UPI se chuka diya`

**Expect:** card *Sunita Devi se payment*, ₹2,100 UPI, Baaki ₹2,100 → **₹0**. **Click** Confirm. ("Sunita Devi ka hisaab barabar.")

### Scene 7: A supplier delivers, partly paid

**Type:** `Balaji Build se 40 box floor tiles aaye 20800 ke, 15000 bank se diye, baaki baad mein`

**Expect:** card *Balaji Build se kharid*: Total ₹20,800 · Abhi diya ₹15,000 bank · Is entry ka baaki ₹5,800 · Aapko Balaji Build ko dena hai **₹5,800** · Floor Tiles 40 → 80 box. **Click** Confirm.

**Say:** "Purchases work the same way. Stock goes up, and what the shop still owes Balaji is tracked."

### Scene 8: An accident in the godown

**Type:** `2 bag wall putty gir ke phat gaye`

**Expect:** card *Stock badlav*: Wajah damaged · Wall Putty 40kg 16 → 14 bag · Kharid rate pe value −₹1,440. **Click** Confirm.

**Say:** "Not every stock change is a sale. Damaged goods leave the stock without touching anyone's khata."

---

## Afternoon

### Scene 9: Who owes whom

**Type:** `Kis kis se paisa lena hai?`

**Expect:** "Total udhaar ₹1,02,820, 7 customers se." and the *Udhaar list*, each customer with a **Remind** button:
- **Verma Constructions**: ₹55,570 baaki · 26 din se
- **Kailash Builders**: ₹17,000 baaki · 28 din se
- **Piyush Agarwal**: ₹8,350 baaki · aaj se
- **Meena Interiors**: ₹7,600 baaki · 19 din se
- **Ramesh Kumar**: ₹7,500 baaki · 22 din se
- **Arif Electricals**: ₹3,500 baaki · 2 din se
- **Deepak Yadav**: ₹3,300 baaki · 4 din se

Footer: "Aapko suppliers ko dena hai: ₹57,400 (Balaji Build, Mahaveer Hardware, Rajasthan Sand Suppliers, Jaipur Paints & Hardware, Shree Traders)."

**Type:** `Mujhe kis kis ko paise dene hain?`

**Expect:** "Aapko 5 suppliers ko kul ₹57,400 dene hain." Shree Traders ₹22,000 · Jaipur Paints & Hardware ₹13,800 · Rajasthan Sand Suppliers ₹9,000 · Mahaveer Hardware ₹6,800 · Balaji Build ₹5,800.

**Optional:** `15 din se purana udhaar` lists only Verma, Kailash, Meena and Ramesh.

**Type:** `Who do I owe the least amount to?`

**Expect,** in English: "You owe Balaji Build the least: ₹5,800" (wording may vary).

**Say:** "That question has no ready-made answer in Arth. ASI1 wrote a database query for it, and the query can only read this shop's books, through read-only views, with a time limit. Every number comes from the database, not from the model."

**Type:** `Piyush se kitna lena hai?`

**Expect:** *Khata: Piyush Agarwal*, "Piyush Agarwal ka baaki: ₹8,350 · Phone +91 98765 43210", with his two entries today: the ₹13,350 sale and the ₹5,000 UPI payment.

### Scene 10: Chasing the biggest udhaar

**Type:** `Verma ko payment reminder bhejo`

**Expect:** "Reminder ka draft. Theek lage to approve kijiye:" and a card *Verma Constructions ko reminder*: Baaki ₹55,570, Bills ARTH/26-27/0001, a polite Hinglish message, and a **UPI QR code**.

**Click** *Approve karke WhatsApp link lo*. **Expect:** a `wa.me` link with the message ready to send, and the UPI payment link for exactly ₹55,570.

**Say:** "Chasing udhaar is the hardest part of running a shop. One tap gives a WhatsApp message with a UPI link for the exact amount. ASI1 writes the words; the amount and bill number are filled in by code."

### Scene 11: A small mistake, fixed

**Type:** `Tempo bhada 800 cash diya`

**Expect:** card *Kharcha*: Transport · ₹800 · cash. **Click** Confirm.

**Type:** `Aaj ka hisaab dikhao`

**Expect:** *Day book: Aaj*, "Bikri ₹39,370 · Aaya ₹9,200 · Kharcha ₹800 · Galle mein cash ₹1,21,960", with every entry of the day: the walk-in sale, Ramesh, Piyush, the Verma bill, Sunita's payment, the Balaji delivery and the tempo.

**Say:** "Wait: the tempo was actually ₹600, not ₹800."

**Type:** `Galti ho gayi, last entry hata do`

**Expect:** "Ye entry hata dun?" and a card *Undo: Kharcha?*. **Click** Undo. **Expect:** "↩ Undo ho gaya: Kharcha. Khata se hata diya."

**Type:** `Tempo bhada 600 cash diya` → **Click** Confirm.

**Say:** "Every confirmed entry can be undone as a whole, across all the agents that wrote it."

---

## Evening

### Scene 12: How did the business do?

**Type:** `इस महीने कितना मुनाफा हुआ?`

**Expect:** two or three sentences from ASI1 with a short tip, and a card *Is mahine ka hisaab*: Bikri ≈ ₹2,60,680, Gross profit ≈ ₹36,260, Kharcha ≈ ₹32,400, Net profit *(≈)*, Lena hai / dena hai **₹1,02,820 / ₹57,400**, with buttons **Remind Verma Constructions (₹55,570)** and **Restock Cement**.

**Say:** "He asked in Hindi script. Profit uses the real cost of every item sold, and all the figures come from SQL. The model only explains them."

**Type:** `Sabse zyada kya bikta hai?`

**Expect:** best sellers this financial year *(≈)*: TMT Bar 12mm on top by value (1,100 kg, ₹74,800); the most margin from Bricks (₹12,000); a row for each item.

**Type:** `Who is my best customer?`

**Expect,** in English: Verma Constructions, ₹80,570 bought this financial year, ₹55,570 still due, with a Remind button.

**Type:** `Is mahine GST kitna banega?`

**Expect:** output GST ≈ ₹39,300 *(≈)*: ₹2,985.26 IGST on the Verma bill plus an estimate for sales made without a bill, and a note that GST paid on purchases (input credit) isn't tracked, so check with a CA before filing.

### Scene 13: Ask anything about the books

None of these has its own feature. ASI1 writes a query each time.

**Type:** `Is mahine ka average bill kitna hai?`
**Expect:** about **77 sales, average bill ≈ ₹3,385** *(≈)*.

**Type:** `Ramesh ne ab tak kitna cement liya hai?`
**Expect:** **30 bags**: 20 earlier this month and 10 today.

**Say:** "Questions nobody programmed in advance still get exact answers."

### Scene 14: Restocking from supplier agents, paid in FET (finale)

**Type:** `Cement khatam hone wala hai, 50 bag mangwa do`

**Expect:** "3 supplier agents se quote liye aur ek round mol-bhav kiya. Kaunsa lein?" and a carousel *50 bag Cement ke quote*:
- **Gupta Cement**: ₹377.30/bag · 3 din · ★4.6, *mol-bhav: ₹385 → ₹377.30*, Total ₹18,865, badges **Best value**, **Top rated**
- **Shree Traders**: ₹392/bag · 2 din · ★4.1, *counter-offer nahi maana*, Total ₹19,600
- **Balaji Build**: ₹398/bag · 1 din · ★3.9, Total ₹19,900, badge **Fastest**

**Say:** "Three independent supplier agents quoted. Arth haggled with the two cheapest, and Gupta Cement accepted 2% less."

**Click** Gupta Cement. **Expect:** "Order check kijiye. Approve karne ke baad payment hoga:" and a card *Restock: Gupta Cement se 50 bag Cement*: Rate ₹377.30 (pehle ₹385)/bag · Order total ₹18,865 · Arth fee (1%) ₹188.65 · **Aap denge ₹19,053.65 = 1.9054 FET (testnet)**.

**Click** *Order approve karo*. **Expect:** "Order Gupta Cement ke saath tay…" and ASI:One's **Approve FET Payment** prompt. **Approve it.**

**Expect,** within a few seconds:
- "✅ Payment mil gaya: 1.9054 FET (explorer link). Gupta Cement ko order aur payment bhej raha hoon…"
- "📦 Order confirmed: 50 bag Cement from Gupta Cement @ ₹377.30 = ₹18,865.", "Supplier ko payment: (explorer link)", "Delivery: (date) tak.", "Stock update: Cement 52 bag bacha."

**Say:** "Two payments on the Fetch.ai testnet. Sharma ji pays Arth; Arth checks that on-chain, then pays the supplier agent, which checks it too and confirms the order. If a supplier fails, the next best is used; if none can deliver, the money is refunded automatically."

### Scene 15: Closing the shop

**Type:** `Stock dikhao`

**Expect:** the stock card again: **Cement 52 bag bacha**, and this time **⚠ White Paint 20L** (3 bucket) is the one running low after Piyush's purchase.

**Say:** "From 12 bags of cement in the morning to 52 by evening, with every rupee and every bag accounted for."

---

## Closing (voice-over)

> "One day, one chat: walk-in and credit sales, a GST bill, payments in and out, a supplier delivery, damaged stock, udhaar reminders on WhatsApp, a day book, profit and GST reports, any question about the books, and restocking from negotiating supplier agents paid on-chain. That's Arth, built with Fetch.ai uAgents, ASI1 and ASI:One."

---

## Optional extras (if you have time, before Scene 14)

| Type | Shows |
|---|---|
| `Cement ka price 430 kar do` → Confirm, then `undo` | Changing the rate list, with undo |
| `Naya item Tile Adhesive 20kg, 450 mein bechenge, 380 ka aata hai` | Adding a product |
| `Hafte ke kis din sabse zyada bikri hoti hai?` | Another SQL answer (a weekday table) |
| `Pichle mahine se sale badhi ya ghati?` | Month-on-month comparison (the demo has little last-month history, so the percentages look extreme) |
| `Last bill dikhao` | The Verma invoice in full, with its PDF link |
| `Ramesh ki aaj wali sale ka bill dikhao` → Bill banao | A GST bill made from Scene 3's khata sale (no bill was made then): same 10 bags and ₹4,200, with the PDF link |
| `What can you do?` | The help card with example buttons |

## Short version (about 4 minutes)

Scenes 1, 3, 4, 5, 9 (first two questions plus "least"), 12 (profit only), 13 (average bill), 14.

## If something goes wrong while recording

| What you see | Why | What to do |
|---|---|---|
| "Abhi samajh nahi paa raha…" or very plain answers | ASI1 quota used up or unreachable (log: `ASI1 unavailable, using fallback`) | Record after the 08:30 IST reset |
| Figures differ from this script | Entries from an earlier rehearsal are still in your shop | `python -m core.db reset`, then open a new chat |
| No reply at all | Agents can't reach Agentverse (log: `Failed to connect to mailbox server`) | Check the internet; restart `honcho` |
| Restock payment fails or no FET prompt | Wallets not funded | Fund both wallets from the Dorado faucet, or end at the carousel |
| A figure differs from this script | The recording date differs (only the *(≈)* ones should), the shop wasn't reset, or a scene was skipped | Reset and replay from Scene 1 |
