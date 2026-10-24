# Deploying Arth

Right now Arth runs on your laptop, so judges can only reach it while the laptop is on, awake and running `honcho start`. Deploying moves everything to always-on machines in the cloud. Nothing about how judges reach Arth changes: they still chat with the same agent on ASI:One.

## What gets deployed

| Piece | Where it runs now | Where it goes | Cost |
|---|---|---|---|
| 10 agents (`honcho start`) | your laptop | **Railway**, one container (the repo's `Dockerfile`) | about $5 a month (Hobby plan); check current pricing |
| Postgres database | Docker on your laptop | **Supabase**, free tier | free |
| Invoice PDFs | Cloudinary | Cloudinary (no change) | free |
| ASI:One / Agentverse mailbox | Agentverse | Agentverse (no change) | free |

The agents only make outgoing connections: to the Agentverse mailbox, ASI1, Supabase, Cloudinary and the Fetch.ai testnet. They talk to each other inside the container on 127.0.0.1. So the server needs **no public URL or open port**.

The orchestrator keeps the same seed, so it keeps the same agent address and the same Agentverse mailbox. You don't reconnect anything on Agentverse.

The Docker image has been built and the full test suite passes inside it (275 passed).

---

## Step 1: Put the code on GitHub

Railway deploys from a GitHub repository.

1. Commit the current work and push it to GitHub. A private repo is fine.
2. Check that **`.env` is not in the repo.** It holds your keys and agent seeds, and `.gitignore` already excludes it. `git status` must never show `.env`.

## Step 2: Create the database (Supabase)

1. Sign up at [supabase.com](https://supabase.com) and create a **New project**.
   - Region: **Mumbai (ap-south-1)**.
   - Database password: choose a strong one and save it.
2. When the project is ready, click **Connect** at the top. Copy the **Session pooler** connection string. It looks like `postgresql://postgres.<project-ref>:<password>@aws-0-ap-south-1.pooler.supabase.com:5432/postgres`.
   - Use the session pooler, not the direct connection, because the direct one only works over IPv6.
   - Put your password in place of `[YOUR-PASSWORD]`.
3. Load the schema and the demo shop from your laptop, in the project folder (PowerShell):

   ```powershell
   $env:DATABASE_URL = "<the Supabase session pooler URL>"
   .venv\Scripts\python -m core.db reset
   Remove-Item Env:DATABASE_URL
   ```

   It prints the party balances when it's done. `reset` wipes the database it points at, which is fine for this new, empty one. The last line makes sure your next commands use your local database again.

If you see an error mentioning `arth_reader` or `permission denied to set role`, stop and send me the message. That role powers the "ask anything" questions, and Supabase sometimes needs one extra grant.

## Step 3: Run the agents (Railway)

1. Sign up at [railway.com](https://railway.com) with your GitHub account. The Hobby plan is needed for an always-on service.
2. **New Project → Deploy from GitHub repo** and pick the Arth repo. Railway finds the `Dockerfile` and builds it.
3. Open the service's **Variables** tab, then **Raw Editor**, and paste the contents of your `.env`, with two changes:
   - Set `DATABASE_URL` to the **Supabase** URL from Step 2.
   - Leave out `TEST_DATABASE_URL`.

   You should end up with `ASI1_API_KEY`, `ASI1_ENABLED`, `DATABASE_URL`, `FET_USE_TESTNET`, `DEMO_INR_PER_FET`, `ARTH_FEE_PCT`, `CLOUDINARY_URL` and all ten `SEED_*` lines.
4. **Settings:**
   - Leave **Networking** without a public domain.
   - Set the **restart policy** to "On failure". If an agent crashes, honcho stops the whole container and Railway starts it again.
5. **Before you deploy, stop Arth on your laptop.** Close the "Arth agents (honcho)" window. Two copies running with the same seeds would both pull messages from the same mailbox, and each would get half of the chat.
6. Deploy, then open **Deployments → View logs**. Within a minute you should see all ten agents start, including the orchestrator registering with Agentverse.

## Step 4: Check it works

1. In ASI:One, open your chat with Arth and send `Cement kitna bacha hai?`. The reply now comes from the server. Try a few lines from `docs/Demo Video Script.md`.
2. Ask for a bill and open its PDF link to check Cloudinary.
3. **Fund the wallets for restocks:** the Procurement wallet address is printed in the Railway logs at startup. It's the same address as on your laptop, because the seed is the same. Fund it, and your ASI:One wallet, from the Dorado testnet faucet.

## Step 5: Make the agent easy to find

On [agentverse.ai](https://agentverse.ai) → **Agents** → the Arth orchestrator:
- **Name:** Arth.
- **Description:** "AI accountant for Indian small shops: khata, GST bills, stock, udhaar reminders, supplier restocking. Hindi, English, Hinglish."
- **README:** paste `agents/orchestrator/README.md`. It should carry the two badges `![tag:innovationlab](https://img.shields.io/badge/innovationlab-3D8BD3)` and `![tag:hackathon](https://img.shields.io/badge/hackathon-5F43F1)`, plus a few example prompts.
- Put the agent's address and an ASI:One chat link in your submission and README.

## After deploying: day-to-day

- **Updating the code:** push to GitHub; Railway rebuilds and redeploys by itself.
- **Resetting the demo shop** (for example right before recording): run Step 2's `reset` command against the Supabase URL again. No restart is needed.
- **Running locally again:** stop the Railway service first (one copy at a time), then use `honcho start` as before. Your local `.env` still points at the laptop database.

---

## Before judging: three things to know

1. **Every judge gets their own demo shop.** Your account owns Sharma Building Supplies. Any other ASI:One account that messages Arth gets a private copy of it, with the same 60 days of history, and is told so in its first reply. Judges can try anything without touching your shop or each other's. To give new users an empty shop instead, set `DEMO_COPY_FOR_NEW_USERS=false`.
2. **ASI1 quota is shared.** Your key allows about 100 ASI1 requests per window (resets 08:30 IST), shared by you and every judge. When it runs out, Arth falls back to keyword rules and many messages get "please try again". A paid or higher-limit ASI1 key avoids this during judging.
3. **Rotate exposed keys.** The Cloudinary key was pasted in a chat earlier. Before going public, create a new API key in Cloudinary, put it in Railway's `CLOUDINARY_URL`, and delete the old key.

## Cheaper alternative: a free virtual machine

If you'd rather not pay for Railway, any Linux VM with 2 GB of memory works. Oracle Cloud's Always Free tier is one option.

```bash
# on the VM, after installing Docker and cloning the repo
cd Arth.Ai
nano .env                     # same contents as on Railway: Supabase DATABASE_URL, no TEST_DATABASE_URL
docker build -t arth .
docker run -d --name arth --restart always --env-file .env arth
docker logs -f arth           # watch the ten agents start
```

The same rules apply: stop the laptop copy first, and nothing needs to be exposed to the internet.
