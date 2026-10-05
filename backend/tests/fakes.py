"""A stand-in for PayPalClient with the same methods, so tests need no network."""

from app.paypal import PayPalError


class FakePayPal:
    def __init__(self):
        self.captured: list[str] = []
        self.voided: list[str] = []
        self.request_ids: list[str] = []
        self.fail_capture: set[str] = set()      # authorization ids PayPal will reject
        self.crash_on_capture: str | None = None  # raise a non-PayPal error once, to simulate a crash
        self.on_authorize = None                  # optional callback to simulate a race
        self.fail_create = False                  # make checkout creation fail
        self.reauthorized: list[tuple[str, str]] = []   # (old id, new id)
        self.fail_reauthorize = False
        self.payouts: list[dict] = []                   # payouts we were asked to send
        self.payout_unavailable = False                 # Payouts not enabled on the account
        self.payout_state = "success"                   # what get_payout answers
        self.webhook_valid = True                 # what signature verification answers
        self.login_profile = {                    # who "PayPal" says just signed in
            "payer_id": "PAYER-1", "email": "sam@example.com", "email_verified": True,
            "name": "Sam Lee", "verified_account": True,
        }
        self.login_fails = False
        self.profile_fails = False

    def create_order(self, *, amount, currency, description, return_url, cancel_url,
                     custom_id=None, request_id=None):
        self.request_ids.append(request_id)
        if self.fail_create:
            raise PayPalError("create order", 500, "boom")
        return f"PPO-{custom_id}", f"https://fake.paypal/approve/{custom_id}"

    def authorize_order(self, paypal_order_id, *, request_id=None):
        self.request_ids.append(request_id)
        if self.on_authorize:
            self.on_authorize()
        return {
            "id": f"AUTH-{paypal_order_id}",
            "status": "CREATED",
            "expiration_time": "2026-11-01T23:29:03Z",
        }

    def capture_authorization(self, authorization_id, *, request_id=None):
        self.request_ids.append(request_id)
        original = authorization_id.removesuffix("-R")  # a re-authorized hold is still the same buyer's hold
        if original == self.crash_on_capture:
            self.crash_on_capture = None
            raise RuntimeError("simulated crash")
        if original in self.fail_capture:
            raise PayPalError("capture authorization", 422, "AUTHORIZATION_EXPIRED")
        self.captured.append(authorization_id)
        return {"id": f"CAP-{authorization_id}", "status": "COMPLETED"}

    def reauthorize_authorization(self, authorization_id, *, request_id=None):
        self.request_ids.append(request_id)
        if self.fail_reauthorize:
            raise PayPalError("reauthorize authorization", 422, "REAUTHORIZATION_NOT_ALLOWED")
        new_id = f"{authorization_id}-R"
        self.reauthorized.append((authorization_id, new_id))
        return {"id": new_id, "status": "CREATED", "expiration_time": "2026-11-20T00:00:00Z"}

    def create_payout(self, *, sender_batch_id, receiver_email, amount, currency, note, sender_item_id):
        if self.payout_unavailable:
            raise PayPalError("create payout", 403, "PERMISSION_DENIED")
        if any(p["sender_batch_id"] == sender_batch_id for p in self.payouts):
            raise PayPalError("create payout", 400, "DUPLICATE_BATCH")  # what PayPal really does
        self.payouts.append({"sender_batch_id": sender_batch_id, "receiver": receiver_email,
                             "amount": amount, "currency": currency})
        return {"batch_id": f"PAYOUT-{sender_batch_id}", "status": "PENDING"}

    def get_payout(self, batch_id):
        return {"status": self.payout_state, "detail": self.payout_state.upper()}

    def void_authorization(self, authorization_id, *, request_id=None):
        self.request_ids.append(request_id)
        self.voided.append(authorization_id)

    def verify_webhook_signature(self, *, headers, event, webhook_id):
        return self.webhook_valid

    def login_url(self, *, redirect_uri, state):
        return f"https://fake.paypal/connect?redirect_uri={redirect_uri}&state={state}"

    def exchange_login_code(self, code):
        if self.login_fails:
            raise PayPalError("exchange login code", 400, "invalid_grant")
        return f"token-for-{code}"

    def get_login_profile(self, access_token):
        if self.profile_fails:
            raise PayPalError("read login profile", 200, "PayPal did not return email. It returned: ['payer_id']")
        return dict(self.login_profile)


class ScriptedLLM:
    """
    A fake language model that plays back replies in order, and records what it was sent.
    Each step is either a string (a text reply) or a list of (tool_name, args) tool requests.
    """

    def __init__(self, *steps):
        self.steps = list(steps)
        self.calls: list[dict] = []  # every generate() call: system, messages, tools

    def generate(self, *, system, messages, tools):
        self.calls.append({"system": system, "messages": list(messages), "tools": tools})
        if not self.steps:
            raise AssertionError("ScriptedLLM ran out of steps")
        step = self.steps.pop(0)
        if isinstance(step, str):
            return {"text": step, "tool_calls": []}
        return {
            "text": None,
            "tool_calls": [
                {"id": f"call-{len(self.calls)}-{i}", "name": name, "args": args}
                for i, (name, args) in enumerate(step)
            ],
        }


def sign_in(client, name="Ann Baker", email="ann@example.com", verified=True, email_verified=True):
    """Sign a TestClient in through the local dev login. Its cookie jar keeps the session."""
    r = client.post("/auth/dev-login", json={"name": name, "email": email, "verified": verified, "email_verified": email_verified})
    assert r.status_code == 200, r.text
    return r.json()


class FakeMailer:
    """Collects emails instead of sending them. Set fail=True to simulate a broken provider."""

    def __init__(self):
        self.sent: list[dict] = []
        self.fail = False

    def send(self, to, subject, body):
        from app.mailer import MailError

        if self.fail:
            raise MailError("SMTPException")
        self.sent.append({"to": to, "subject": subject, "body": body})

    def last_code(self) -> str:
        import re

        return re.search(r"\b(\d{6})\b", self.sent[-1]["body"]).group(1)


class FakeGeocoder:
    """
    Address lookups without the network. Put a word in the address to steer the answer:
    "nowhere" is not found, "down" simulates the service being down, "far" is ~30 miles from the
    shop, "near" is ~1 mile away. Anything else is the shop's own address.
    """

    def __init__(self):
        self.calls: list[str] = []

    def lookup(self, query):
        from app.geo import GeocodeError, Place

        self.calls.append(query)
        q = query.lower()
        if "nowhere" in q:
            return None
        if "down" in q:
            raise GeocodeError("simulated outage")
        if "far" in q:
            return Place(41.15, -73.9857, "Faraway, New York", "A far away address")
        if "near" in q:
            return Place(40.7580, -73.9855, "Midtown, New York", "A nearby address")
        return Place(40.7484, -73.9857, "Koreatown, New York", "350 5th Avenue, New York, NY")
