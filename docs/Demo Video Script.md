# Arth: Demo Video Script (about 5 minutes)

One working day at **Sharma Building Supplies**, a building-materials shop in Jaipur, told in 10 short scenes. Every message below was run end to end against the live ASI1 model and the real agents on a fresh copy of the demo shop, and the replies shown are what Arth returned.

**How to read this script:**
- **Type** is what you send in ASI:One.
- **Click** is a button on Arth's card.
- **Expect** is the verified reply.
- **Say** is your voice-over.

Figures marked *(varies)* depend on the recording date or on ASI1's wording; everything else is exact.

## Before you record

1. **Fresh demo shop.** Run this, then restart the agents (`honcho start`, all 10 should log "status updated to active"):

   ```
   python -m core.db reset
   ```

2. **Connect.** Open ASI:One, start a chat with the Arth agent, and send `hi` once off camera. The first message claims the demo shop. Then start a new chat for the recording.
3. **WhatsApp on camera.** Verma's number in the demo data is made up. Off camera, send `Verma Constructions ka number <your second phone or a teammate's number> hai` and confirm, so the WhatsApp link in Scene 7 opens a real chat. Don't use a stranger's number.
4. **Testnet FET for the finale.**
   - Fund the **Procurement wallet** with at least 2 testnet FET; its address is printed when the procurement agent starts.
   - Fund your **ASI:One wallet** with at least 2.1 testnet FET.
   - Both come from the Fetch.ai Dorado faucet.
   - Without funds, end the video at the order card in Scene 10.
5. **ASI1 quota.** The whole video uses about 25 ASI1 requests, and the key allows about 100 per window (resets 08:30 IST). Don't rehearse the full script three times right before recording.
6. **Screen.** Have the browser zoomed to about 110%, notifications off, and the architecture diagram (`docs/download.png`) open in another tab for the closing.

---

## Scene 0: The hook (0:00 to 0:20)

Show ASI:One with the Arth agent open.

**Say:** "Millions of small shops in India still run on a paper khata: credit written by hand, GST bills made manually, stock counted by eye. Meet Arth, an AI accountant that lives in chat. The shopkeeper just says what happened, in Hindi, English or Hinglish, and a team of Fetch.ai agents keeps the books. Let's spend a day at Sharma Building Supplies."

## Scene 1: Opening the shop (0:20 to 0:35)

**Type:** `Cement kitna bacha hai?`

**Expect:** "Cement: 12 bag bacha hai. ⚠ Kam hai (reorder level 20)."

**Say:** "First thing in the morning, Mr. Sharma checks his stock. Cement is already below the reorder level. Remember that for the end of the video."

## Scene 2: A sale in one sentence (0:35 to 1:05)

**Type:** `Ramesh ko 10 bag cement becha 4200 mein, aadha cash mila`

**Expect:** a review card, *Ramesh Kumar ko bikri*:
- Items: 10 bag Cement · Total ₹4,200
- Abhi mila ₹2,100 cash · Is entry ka baaki ₹2,100
- Ramesh Kumar ka total baaki **₹7,500**
- Stock: Cement 12 → 2 bag

**Click** Confirm.

**Expect:** "✅ Ho gaya! Khata update ho gaya. Ramesh Kumar ka total baaki: ₹7,500. Stock update: Cement 2 bag bacha. Ramesh Kumar ko 31 Oct ko yaad dilaunga *(date varies)*. ⚠ Cement kam hai: sirf 2 bag bacha."

**Say:** "One Hinglish sentence. ASI1 understood that 'aadha cash' means half paid. Code worked out the ₹2,100, updated Ramesh's khata and the stock, and nothing was saved until he tapped Confirm. Behind this card, three agents (Ledger, Inventory and Collections) planned and committed together."

## Scene 3: Arth asks when it isn't sure (1:05 to 1:30)

**Type:** `Kailash Builders ko 200 kg sariya becha, udhaar mein`

**Expect:** "TMT Bar 12mm ya TMT Bar 8mm? Kaun sa?"

**Type:** `12mm`

**Expect:** a card *Kailash Builders ko bikri*: 200 kg TMT Bar 12mm · Total ₹13,600 (from the rate list) · ₹0 (udhaar) · Kailash Builders ka total baaki **₹30,600** · TMT Bar 12mm 850 → 650 kg.

**Click** Confirm.

**Say:** "'Sariya' is what shopkeepers call TMT bars, and the shop stocks two sizes. Instead of guessing, Arth asks. A one-word answer continues the same sale, and the price comes from the shop's own rate list."

## Scene 4: A GST bill with a PDF (1:30 to 2:15)

**Type:** `Verma Constructions ka bill banao: 200 kg TMT 12mm aur 100 kg TMT 8mm`

**Expect:** a draft *Verma Constructions ka GST bill*: ARTH/26-27/0001 · 200 kg TMT Bar 12mm @ ₹68 = ₹13,600 · 100 kg TMT Bar 8mm @ ₹70 = ₹7,000 · **IGST ₹3,142.38** · Bill total ₹20,600.

**Type:** `5% discount do`

**Expect:** the draft re-priced: ₹12,920 and ₹6,650 · Discount 5% · Taxable value ₹16,584.74 · IGST ₹2,985.26 · **Bill total ₹19,570** · Verma's total baaki ₹55,570.

**Click** *Confirm karke bill banao*.

**Expect:** "✅ Ho gaya! Bill ARTH/26-27/0001 ban gaya. Khata update ho gaya. Verma Constructions ka total baaki: ₹55,570…" and "📄 Bill ARTH/26-27/0001 ka PDF: (link)". **Open the link** and show the tax invoice: GSTIN, HSN codes, the IGST line and the amount in words.

**Say:** "Verma is in Haryana, so Arth charged IGST instead of CGST and SGST, all by itself. He edited the bill just by chatting, and the finished GST invoice is a PDF he can send right away."

## Scene 5: English works too, and Arth catches a double entry (2:15 to 2:50)

**Type:** `Meena Interiors paid 5000 by UPI`

**Expect** (in English): card *Payment from Meena Interiors*: ₹5,000 · UPI · They owe ₹7,600 → ₹2,600. **Click** Confirm. "✅ Done! Khata updated. Meena Interiors now owes ₹2,600 in total."

**Type:** `Meena ne 5000 UPI se diye`

**Expect:** a card with two warnings:
- "⚠ Baaki se zyada: Meena Interiors ka sirf ₹2,600 baaki tha; ₹2,400 advance ban jayega"
- "⚠ Shayad dobara: Yahi payment abhi abhi likha gaya tha"

The button now reads *Phir bhi likho*. **Click** Cancel. "Theek hai, cancel kar diya. Kuch nahi likha."

**Say:** "He switched to English, so Arth answered in English. Then a helper typed the same payment again in Hinglish. Arth noticed it was a duplicate, and more than Meena owed, and stopped it before it reached the books."

## Scene 6: Who owes me money? (2:50 to 3:10)

**Type:** `Kis kis se paisa lena hai?`

**Expect:** "Total udhaar ₹1,05,170, 7 customers se." and the *Udhaar list*, each with a **Remind** button:
- Verma Constructions ₹55,570 · 26 din se
- Kailash Builders ₹30,600 · 28 din se
- Ramesh Kumar ₹7,500 · 22 din se
- Arif Electricals ₹3,500 · 2 din se
- Deepak Yadav ₹3,300 · 4 din se
- Meena Interiors ₹2,600 · 19 din se
- Sunita Devi ₹2,100 · 9 din se

Footer: "Aapko suppliers ko dena hai: ₹51,600 (…)".

**Say:** "Every rupee of credit, who owes it and for how long, and a one-tap reminder for each."

## Scene 7: A reminder on WhatsApp (3:10 to 3:40)

**Click** *Remind* on **Verma Constructions**.

**Expect:** the card *Verma Constructions ko reminder*:
- Baaki ₹55,570 · Bills ARTH/26-27/0001
- A polite Hinglish message written by ASI1 *(wording varies)*
- A **UPI QR code** for exactly ₹55,570

**Click** *Approve karke WhatsApp link lo*. **Expect:** "✅ Ho gaya! WhatsApp pe bhejne ke liye kholiye: https://wa.me/…" plus the UPI link.

**Open the WhatsApp link.** WhatsApp opens the chat with the message already typed, including the bill number, the amount and the UPI payment link.

**Say:** "Chasing credit is the hardest part of running a shop. ASI1 writes a polite message; code fills in the exact amount, the bill number and a UPI link. One tap and it's ready to send on WhatsApp."

## Scene 8: Questions nobody programmed (3:40 to 4:15)

**Type:** `Who do I owe the least amount to?`

**Expect:** "You owe Mahaveer Hardware the least, ₹6,800."

**Type:** `Hafte ke kis din sabse zyada bikri hoti hai?`

**Expect:** a weekday table, for example "Somvaar ko sabse zyada bikri hoti hai, ab tak ₹96,440 ki 16 bills." *(figures vary with the date)*

**Say:** "Arth has no ready-made report for 'least' or for weekdays. ASI1 writes a database query on the spot. A guard lets it read only this shop's books, read-only, with a time limit. So every number comes from the database, never from the model's imagination."

## Scene 9: How's business? (4:15 to 4:35)

**Type:** `इस महीने कितना मुनाफा हुआ?`

**Expect:** two or three sentences from ASI1 with a tip *(wording varies)*, and the card *Is mahine ka hisaab*: Bikri, Gross profit, Kharcha, Net profit *(figures vary with the date)*; **Lena hai / dena hai ₹1,05,170 / ₹51,600**; buttons **Remind Verma Constructions (₹55,570)** and **Restock Cement**.

**Say:** "He asked in Hindi script. Profit uses the real cost of every item sold. The model only explains the numbers, and even suggests what to do next."

## Scene 10: Agents that negotiate and pay (4:35 to 5:30)

**Type:** `50 bag cement mangwa do`

**Expect:** "3 supplier agents se quote liye aur ek round mol-bhav kiya. Kaunsa lein?" and a carousel:

| Supplier | Price | Delivery | Rating | Total | Note |
|---|---|---|---|---|---|
| **Gupta Cement** | ₹377.30/bag | 3 din | ★4.6 | ₹18,865 | *mol-bhav: ₹385 → ₹377.30*; badges **Best value**, **Top rated** |
| **Shree Traders** | ₹392/bag | 2 din | ★4.1 | ₹19,600 | *counter-offer nahi maana* |
| **Balaji Build** | ₹398/bag | 1 din | ★3.9 | ₹19,900 | badge **Fastest** |

**Say:** "Three independent supplier agents quoted. Arth's procurement agent haggled with the two cheapest. Gupta Cement accepted 2% less, and Shree Traders refused."

**Click** Gupta Cement.

**Expect:** the card *Restock: Gupta Cement se 50 bag Cement*:
- Rate ₹377.30 (pehle ₹385)/bag · Order total ₹18,865 · Arth fee (1%) ₹188.65
- **Aap denge ₹19,053.65 = 1.9054 FET (testnet)** · Delivery 3 din mein

**Click** *Order approve karo*. **Expect:** "Order Gupta Cement ke saath tay… Ab ₹19,053.65 = 1.9054 FET ka payment approve kijiye", then ASI:One's **Approve FET Payment** prompt. **Approve it.**

**Expect:**
- "✅ Payment mil gaya: 1.9054 FET (explorer link)"
- "📦 Order confirmed: 50 bag Cement from Gupta Cement @ ₹377.30 = ₹18,865. Supplier ko payment: (explorer link). Delivery: (date). Khata update ho gaya. Gupta Cement ka hisaab barabar. Stock update: **Cement 52 bag bacha.**"

**Open one explorer link** to show the real transfer on the Fetch.ai testnet.

**Say:** "He paid Arth in FET, and Arth checked the payment on-chain. Then Arth's procurement agent paid the supplier agent, which checked the payment itself before confirming. Stock and khata updated automatically. Agents negotiating and paying each other: that's the agent economy, for a small shop."

## Closing (5:30 to 5:50)

Show the architecture diagram.

**Say:** "That's Arth: ten Fetch.ai agents behind one chat. An orchestrator talks to the owner, six specialists keep the books, and three supplier agents sell to the shop. ASI1 understands Hindi, English and Hinglish, ASI:One cards make every step one tap, and the Payment Protocol moves real testnet FET. A full accounts team that speaks the shopkeeper's language."

---

## If something goes off script

- **Arth says "Abhi samajh nahi paa raha, thodi der mein phir try kijiye."** ASI1 is out of quota or unreachable. Wait, or record after 08:30 IST when the quota resets.
- **A card asks a different question.** Answer it in a word; Arth continues the same entry.
- **You confirmed something by mistake.** Type `undo`; the whole entry is reversed across all agents.
- **The payment prompt doesn't appear, or "payment verify nahi hua".** Check that both wallets have FET, then type `50 bag cement mangwa do` again. The earlier order expires after 10 minutes.
