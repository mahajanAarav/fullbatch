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
