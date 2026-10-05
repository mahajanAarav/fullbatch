# fullbatch

An AI agent that runs preorder "drops" for small sellers such as home bakers and market vendors. Buyers order in chat, PayPal places a hold on each payment, and buyers are only charged if the drop reaches its minimum by the deadline. After each drop, the agent reports the results and recommends the next one.

## How a drop works

1. The seller messages the agent with the item, quantity, price, minimum orders, and deadline.
2. Buyers order in chat. The agent reserves stock and sends a PayPal approval link.
3. When the buyer approves, the backend authorizes the payment (a hold, no charge).
4. At the deadline, if the minimum is met, every authorization is captured. If not, every authorization is voided.
5. Drop planner: the agent summarizes the drop and recommends quantity, price, and timing for the next one.

## Stack

- Backend: Python, FastAPI
- PayPal: direct REST calls with `httpx` (Orders API v2 with `intent: AUTHORIZE`, Payments API v2 for capture and void, webhooks for payment events)
- Database: Postgres
- AI: an LLM API with tool calling; the agent's tools are backend functions (create drop, take order, check stock)
- Frontend: React, with AG Studio (AG Grid) for the seller dashboard (`frontend/src/components/Dashboard.tsx`, lazy-loaded; data from `GET /me/analytics`, which never includes buyer identities)
- Chat channel: a web chat inside the React app, optional: everything it does also has buttons and forms
- Accounts: Log in with PayPal (PayPal verifies identity; sellers need a PayPal-verified account), cookie sessions
- LLM providers: Gemini models first, Groq as the last-resort fallback, behind one small interface (`app/llm.py`)
- Hosting: Render, with Render Workflows for the deadline job

## Layout

```
backend/
  app/
    main.py       API routes and PayPal webhooks
    auth.py       sign-in: PayPal login, dev login, cookie sessions, access checks
    paypal.py     authorize, capture, void, plus Log in with PayPal calls
    drops.py      drop engine: stock, minimum, deadline, settlement, seller verification
    geo.py        address lookup (free OpenStreetMap Nominatim, cached, rate limited) and distance math
    studio_ai.py  server side of AG Studio's assistant (POST /ai/turn): guarded, rate limited, runs our Groq-then-Gemini chain
    planner.py    drop planner: per-drop reports + explainable next-drop recommendations (numbers from code, not the LLM)
    agent.py      chat agent and its role-scoped tools
    llm.py        provider-neutral model interface + fallback;  gemini.py, groq.py, llm_factory.py
    models.py     database tables;  db.py, config.py, deps.py
    server.py     deployed shape: API under /api + the built React app, in-app deadline timer
  migrations/     Alembic
  scripts/        dev_db.py, sandbox checks, live model check
  tests/          pytest (real Postgres via pixeltable-pgserver, fake PayPal)
frontend/         React + Vite + TypeScript. Pages: Home (the marketplace, no hero), Orders, OrderStatus, Sell (Drops / Planner / Dashboard tabs), SignIn
workflows/        (empty) reserved for Render Workflows
Dockerfile, render.yaml, DEPLOY.md   deployment (one Render web service)
dev.sh            starts the database, API and frontend locally
```

## Commands

- Run everything locally: `./dev.sh` (sets `DEV_LOGIN=1`, a local-only sign-in; open http://localhost:5173)
- Tests: `cd backend && source .venv/bin/activate && python -m pytest tests -q`
- Python environment: `source backend/.venv/bin/activate` (dev deps: `pip install -r backend/requirements-dev.txt`)
- PayPal sandbox checks: `python backend/scripts/check_paypal_credentials.py`; the flow scripts are interactive
  (a person must approve the payment as a sandbox buyer), so do not run `test_paypal_flow.py` or `test_paypal_client.py` unattended.
- Demo data for the dashboard and screenshots (local DB only): `python backend/scripts/seed_demo.py`, then dev-sign-in as demo@example.com (tick verified)
- The dashboard's AI assistant (AG Studio Agent Framework): a custom "Drop planner" agent in `frontend/src/studio/` (adapter.ts talks to /api/ai/turn; plannerAgent.ts defines the agent and its tools). It appears in Studio's edit mode ("Customize & AI planner"). It can open the New drop form pre-filled but never creates a drop.
- Live model check: `python backend/scripts/check_gemini_agent.py [--provider gemini|groq|auto]`

## Rules

- PayPal sandbox only. The base URL is `https://api-m.sandbox.paypal.com`. Never call the live API.
- Secrets live in `.env`, which is git-ignored. Never print, log, echo, or commit their values. `.env.example` lists the variable names only.
- Send a unique `PayPal-Request-Id` header on every PayPal POST so retries cannot double-charge.
- Stock reservation must use row locking so two buyers cannot claim the last unit.
- The LLM never calls PayPal directly. Capture, void, and refund go through functions in `backend/app/paypal.py`.
- Keep the PayPal functions small and reusable: `get_token`, `create_order`, `authorize_order`, `capture_authorization`, `void_authorization`.
- Who is calling comes from the sign-in cookie, never from a request body or from the LLM. Sellers act only on their own drops; buyers see only their own orders.
- A drop's exact pickup address and notes, and a buyer's delivery address, are private: public data carries only a neighborhood label and coordinates rounded to ~1 km. The exact address is revealed only to the buyer whose hold is approved; sellers see delivery addresses only for approved orders.
- Only shops whose owner has a PayPal-verified account may open drops. The rule lives in `drops.create_drop`, so every path is covered.
- `DEV_LOGIN` (the local sign-in that skips PayPal) must never be set on the deployed app.
- Commit in small steps with clear messages.
