# Deploying fullbatch

The whole app is **one Render web service** (the API under `/api` plus the React app) and **one Postgres database**.
Total cost: **$0** on free tiers. Read the two warnings first.

> **Two free-tier traps**
> 1. **Render's free Postgres is deleted after 30 days.** Use Neon's free Postgres instead (below). It does not expire.
> 2. **A free Render web service sleeps after 15 minutes without traffic** and takes about a minute to wake. The deadline
>    timer lives inside the app, so it only runs while the app is awake. Step 5 keeps it awake.

Secrets (keys, passwords) go into the Render dashboard only. They are never committed to the repo.

## 1. Database (Neon, free)

1. Sign up at neon.tech and create a project (any name, the default region is fine).
2. Copy the **connection string** (it looks like `postgresql://user:password@host/dbname?sslmode=require`).
   Keep it handy for step 2. The app creates its own tables on first start.

## 2. Create the Render service

1. In Render: **New → Blueprint**, connect your GitHub account, and pick the `fullbatch` repository.
   Render reads `render.yaml` and proposes one web service.
2. It asks for these values. Copy them from your local `.env` (and Neon for the first one):

   | Variable | Value |
   |---|---|
   | `DATABASE_URL` | the Neon connection string |
   | `PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET` | the same sandbox values as your `.env` |
   | `GEMINI_KEY`, `GROQ_KEY` | the same as your `.env` |
   | `PUBLIC_API_URL`, `FRONTEND_URL`, `PAYPAL_WEBHOOK_ID` | leave blank. **Render drops blank variables**, so you will add them by hand in steps 3 and 4 |

3. Create it and wait for the first deploy (a few minutes). Render shows your address, like
   `https://fullbatch.onrender.com`. **If the name was taken it adds a suffix. Use whatever it shows.**

## 3. Tell the app its own address

In the service's **Environment** tab, click **Add variable** for each of these (they will not be there yet, because Render drops blank
variables). Replace `YOUR-URL`, use **https**, and leave **no trailing slash**:

- `PUBLIC_API_URL` = `https://YOUR-URL/api`
- `FRONTEND_URL` = `https://YOUR-URL`

Save. Render redeploys.

## 4. PayPal dashboard (sandbox app)

1. **Log in with PayPal** settings: set the **Return URL** to `https://YOUR-URL/api/auth/paypal/callback`.
   For the Privacy Policy and User Agreement URLs you can use `https://YOUR-URL`.
   Make sure the scopes for name, email and account verification status are ticked.
2. **Webhooks**: add a webhook with URL `https://YOUR-URL/api/paypal/webhook` and tick at least
   **Checkout order approved**. Copy its **Webhook ID**.
3. Back in Render, **add** a variable `PAYPAL_WEBHOOK_ID` with that ID. Save.

## 5. Keep it awake (free)

Create a free monitor at uptimerobot.com: type **HTTP(s)**, URL `https://YOUR-URL/api/health`, every **5 minutes**.
(One always-on free service uses about 744 of the 750 free hours in a month.)
If you would rather not depend on a pinger, Render's paid Starter plan does not sleep.

## 6. Check it works

- Open `https://YOUR-URL`. The home page loads.
- Click **Sign in → Continue with PayPal** and sign in with a sandbox account.
  (The "Developer sign-in" box does **not** appear on the deployed app, on purpose.)
- A sandbox **Business** account that PayPal reports as verified can open a shop and create drops.
- Place an order as a sandbox **Personal** account and approve it. You should land on the "You're in!" page.

## 7. Email (free), for verification codes

Buyers and sellers whose email PayPal has not already confirmed get a 6-digit code by email. Without email set up, the deployed
site says "Email verification isn't set up yet" to those people (people whose email PayPal confirmed are not affected).
Any SMTP provider works. Pick one:

**Option A: Gmail (simplest if you have a Gmail account)**
1. Turn on 2-Step Verification for the Google account (myaccount.google.com, Security).
2. Create an **App password** (Security, then 2-Step Verification, then App passwords). Copy the 16-character password.
3. In Render, **add** these variables (they will not be there, because Render drops blank ones):

   | Variable | Value |
   |---|---|
   | `SMTP_HOST` | `smtp.gmail.com` |
   | `SMTP_USER` | your Gmail address |
   | `SMTP_PASSWORD` | the 16-character app password (no spaces) |
   | `EMAIL_FROM` | your Gmail address |

   `SMTP_PORT` is already `587`. Gmail allows roughly 500 emails a day, far more than a demo needs.

**Option B: Brevo (free, 300 emails a day, no Gmail needed).** Create an account at brevo.com, verify a sender address,
create an SMTP key under SMTP & API, then set `SMTP_HOST=smtp-relay.brevo.com`, `SMTP_USER` to the login Brevo shows,
`SMTP_PASSWORD` to the SMTP key, and `EMAIL_FROM` to your verified sender address.

Save, let Render redeploy, then sign in with an account whose email is unconfirmed and press Reserve to test it.
Treat the app password / SMTP key like any other secret. It goes only in Render.

## Notes

- The Docker build has not been run on the author's machine (no Docker installed). The same steps were checked separately:
  the frontend builds, the runtime-only Python requirements install into a clean environment, and the server runs and serves
  both the API and the app. If the first Render build fails, the build log will say why. Send it over.
- `DEV_LOGIN` must stay unset in production.
