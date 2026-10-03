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
- Frontend: React, with AG Studio (AG Grid) for the seller dashboard
- Chat channel: a web chat inside the React app
- Hosting: Render, with Render Workflows for the deadline job

## Layout

```
backend/
  app/
    main.py       API routes and PayPal webhooks
    paypal.py     authorize, capture, void, refund
    drops.py      drop engine: stock, minimum, deadline
    agent.py      chat agent and its tools
    models.py     database tables
  scripts/
    test_paypal_flow.py   sandbox test of authorize, capture, void
  requirements.txt
frontend/         React app
workflows/        Render Workflows tasks
```

## Commands

- Activate the Python environment: `source backend/.venv/bin/activate`
- PayPal flow test: `python backend/scripts/test_paypal_flow.py`
  This script is interactive. A person must open the printed link and approve the payment as a sandbox buyer, so do not run it unattended.

## Rules

- PayPal sandbox only. The base URL is `https://api-m.sandbox.paypal.com`. Never call the live API.
- Secrets live in `.env`, which is git-ignored. Never print, log, echo, or commit their values. `.env.example` lists the variable names only.
- Send a unique `PayPal-Request-Id` header on every PayPal POST so retries cannot double-charge.
- Stock reservation must use row locking so two buyers cannot claim the last unit.
- The LLM never calls PayPal directly. Capture, void, and refund go through functions in `backend/app/paypal.py`.
- Keep the PayPal functions small and reusable: `get_token`, `create_order`, `authorize_order`, `capture_authorization`, `void_authorization`.
- Commit in small steps with clear messages.
