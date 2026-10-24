# Deploying Arth

Arth is deployed as one always-on service on **Railway**, with Railway's own Postgres next to it. The orchestrator stays registered on **Agentverse** through its Mailbox, which is how ASI:One reaches it. The code is on GitHub at [suppiyush/arth.ai](https://github.com/suppiyush/arth.ai).

| Piece | Where it runs | Cost |
|---|---|---|
| 10 agents | Railway, one container built from the repo's `Dockerfile` | Hobby plan, about $5 a month; check current pricing |
| Postgres | Railway Postgres, in the same project | included in Railway usage |
| Invoice PDFs | Cloudinary (unchanged) | free |
| ASI:One access | the orchestrator's Agentverse Mailbox (unchanged) | free |

How the deployment fits together:
- **No public URL needed.** The agents only make outgoing connections: to the Agentverse mailbox, ASI1, Postgres, Cloudinary and the Fetch.ai testnet. They talk to each other inside the container.
- **Automatic database setup.** On the first start the container creates the tables and the demo shop by itself (`python -m core.db init`). Later starts leave the data alone.
- **Same agent address.** The orchestrator keeps its seed, so it keeps its address (`agent1qvch7u0jna59nw4jkpc8kw3ljjclausz3lrqxn9f8xtuqkryvws9gfsln74`) and its Agentverse mailbox. There's nothing to reconnect.
- **Tested.** The image was built and started on an empty database: the shop was created and all 10 agents came up.

---

## Step 1: Create the Railway project

1. Sign up at [railway.com](https://railway.com) with your GitHub account (`suppiyush`). Choose the **Hobby** plan, which is needed for a service that stays on.
2. **New Project → Deploy from GitHub repo**, then pick **suppiyush/arth.ai**. If the repo isn't listed, click **Configure GitHub App** and give Railway access to it. Railway starts a first build, which will fail or wait until the variables are in. That's expected.
3. In the same project click **+ New → Database → Add PostgreSQL**.

## Step 2: Add the settings

Open the **arth.ai** service (not Postgres), go to **Variables → Raw Editor**, and paste the contents of your local `.env` file. Then change two lines:

```
DATABASE_URL=${{Postgres.DATABASE_URL}}
```

and **delete** the `TEST_DATABASE_URL` line. The `${{Postgres.DATABASE_URL}}` text is typed exactly like that: Railway fills in the address of the Postgres you just added.

Check that these are all there:
- `ASI1_API_KEY`, `ASI1_ENABLED`
- `DATABASE_URL`
- `FET_USE_TESTNET`, `DEMO_INR_PER_FET`, `ARTH_FEE_PCT`
- `CLOUDINARY_URL`
- all ten `SEED_*` lines

Click **Update Variables**.

## Step 3: Stop Arth on your laptop

Close the **"Arth agents (honcho)"** window. Two copies running with the same seeds would both read the orchestrator's mailbox, and each would get half of the chat. From now on, Railway is the only copy.

## Step 4: Deploy

1. In the service's **Settings**:
   - Check that **Networking** has no public domain; none is needed.
   - Set **Restart Policy** to **On Failure**.
2. Go to **Deployments** and click **Deploy** (or **Redeploy**).
3. Open **View logs**. Within about a minute you should see:
   - `database created with the demo shop` (first deploy only)
   - ten lines `Starting server on http://0.0.0.0:8001` … `8010`, one per agent
   - the orchestrator connecting to its Agentverse mailbox

## Step 5: Check Agentverse and ASI:One

1. On [agentverse.ai](https://agentverse.ai), open **Agents → Arth**. It should show **Mailbox** and **Active**.
   - On its **Overview**, add an avatar and a handle such as `@arth`. The description and README (with the `innovationlab` and `hackathon` badges) are sent from the code on every start.
   - The **Protocols** tab should list the Chat Protocol and the Payment Protocol.
2. Click **Chat with agent**. ASI:One opens. Send `Kis kis se paisa lena hai?` and expect the dues list with 7 customers.
3. Copy that chat's link: it's the **ASI:One chat URL** for the submission.
4. **For restocks:** the Procurement wallet address appears in the Railway logs when the procurement agent starts; it's the same address as before. Fund it, and your ASI:One wallet, from the Dorado testnet faucet.

## What to submit

- **ASI:One chat URL:** from Step 5.
- **Agentverse agent URL:** the Arth page on agentverse.ai.
- **GitHub:** https://github.com/suppiyush/arth.ai. The README lists all 10 agent addresses and has the badges.
- **Demo video:** see `docs/Demo Video Script.md`.

---

## Day to day

- **Update the code:** push to `main` on suppiyush/arth.ai, and Railway rebuilds and redeploys by itself.
- **Reset the demo shop** (for example before recording): in Railway, open the **Postgres** service, then **Data**. Open its **Connect** tab and copy the **public** connection URL. Then, on your laptop:

  ```powershell
  $env:DATABASE_URL = "<Railway Postgres public URL>"
  .venv\Scripts\python -m core.db reset
  Remove-Item Env:DATABASE_URL
  ```

  `reset` wipes that database and reloads the demo shop. No redeploy is needed. Every ASI:One user except the first gets a private copy of the demo shop, so judges never see each other's entries.
- **Run locally again:** first remove or pause the Railway deployment (one copy at a time), then use `honcho start` as before. Your local `.env` still points at the laptop database.

## Before judging

- **ASI1 quota:** your key allows about 100 ASI1 requests per window (resets 08:30 IST), shared by everyone using Arth. When it runs out, Arth falls back to keyword rules and many messages get "please try again". A higher-limit key avoids this during judging.
- **Rotate the Cloudinary key:** it was pasted into a chat earlier. Create a new API key in Cloudinary, update `CLOUDINARY_URL` in Railway, and delete the old key.

## Other ways to host

- **A free VM** (for example Oracle Cloud's Always Free tier) with Docker:

  ```bash
  git clone https://github.com/suppiyush/arth.ai && cd arth.ai
  nano .env                      # your settings, DATABASE_URL pointing at a Postgres you run or rent
  docker build -t arth .
  docker run -d --name arth --restart always --env-file .env arth
  docker logs -f arth
  ```

- **Supabase instead of Railway Postgres:** use its **Session pooler** URL as `DATABASE_URL`. If the logs mention `arth_reader` or `permission denied to set role`, that role needs one extra grant on Supabase; ask before working around it.
