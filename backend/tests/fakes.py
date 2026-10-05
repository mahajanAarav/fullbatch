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
        self.webhook_valid = True                 # what signature verification answers
        self.login_profile = {                    # who "PayPal" says just signed in
            "payer_id": "PAYER-1", "email": "sam@example.com", "email_verified": True,
            "name": "Sam Lee", "verified_account": True,
        }
        self.login_fails = False

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
        if authorization_id == self.crash_on_capture:
            self.crash_on_capture = None
            raise RuntimeError("simulated crash")
        if authorization_id in self.fail_capture:
            raise PayPalError("capture authorization", 422, "AUTHORIZATION_EXPIRED")
        self.captured.append(authorization_id)
        return {"status": "COMPLETED"}

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
