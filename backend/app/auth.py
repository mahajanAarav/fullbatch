"""
Sign-in: PayPal login (and a local-only dev login), cookie sessions, and the checks routes use.

How it works:
  1. The browser is sent to PayPal. PayPal sends it back here with a one-time code.
  2. We trade the code for the person's profile, create or update their User, and start a
     session: a random token goes into an httpOnly cookie, and only its SHA-256 hash is stored.
  3. Routes learn who is calling from that cookie, never from anything the browser claims.
"""

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import get_session
from app.deps import get_mailer, get_paypal
from app.mailer import MailError
from app.models import AuthSession, EmailCode, Seller, User
from app.paypal import PayPalError
from app.ratelimit import RateLimiter

SESSION_COOKIE = "fb_session"
STATE_COOKIE = "fb_oauth_state"
NEXT_COOKIE = "fb_oauth_next"
SESSION_DAYS = 14
STATE_MINUTES = 10
CODE_MINUTES = 10        # how long an emailed code works
CODE_MAX_ATTEMPTS = 5    # wrong guesses before the code is dead
RESEND_SECONDS = 60      # the shortest wait between two codes
send_limiter = RateLimiter(limit=5, window=3600)  # emails per person per hour

router = APIRouter(prefix="/auth", tags=["auth"])


def _now() -> datetime:
    return datetime.now(timezone.utc)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def start_session(db: Session, user: User, now: datetime | None = None) -> str:
    """Create a session and return the raw token, which exists only in the cookie."""
    now = now or _now()
    token = secrets.token_urlsafe(32)
    db.add(AuthSession(token_hash=hash_token(token), user_id=user.id, expires_at=now + timedelta(days=SESSION_DAYS)))
    db.commit()
    return token


def user_for_token(db: Session, token: str | None, now: datetime | None = None) -> User | None:
    if not token:
        return None
    now = now or _now()
    row = db.scalar(select(AuthSession).where(AuthSession.token_hash == hash_token(token)))
    if row is None or row.expires_at <= now:
        return None
    return db.get(User, row.user_id)


def get_current_user(request: Request, db: Session = Depends(get_session)) -> User | None:
    """Who is calling, or None if nobody is signed in."""
    return user_for_token(db, request.cookies.get(SESSION_COOKIE))


def require_user(user: User | None = Depends(get_current_user)) -> User:
    if user is None:
        raise HTTPException(401, "Please sign in first.")
    return user


def require_verified_email(user: User = Depends(require_user)) -> User:
    """Reserving and opening a shop need a confirmed email, so there is always a way to reach people."""
    if not user.email_verified:
        raise HTTPException(403, "Please verify your email first.")
    return user


def shop_of(db: Session, user: User) -> Seller | None:
    return db.scalar(select(Seller).where(Seller.user_id == user.id))


def require_shop(user: User = Depends(require_user), db: Session = Depends(get_session)) -> Seller:
    shop = shop_of(db, user)
    if shop is None:
        raise HTTPException(403, "Create your shop first.")
    return shop


def buyer_chat_id(user: User) -> str:
    """The chat/order session id for a signed-in buyer. Derived on the server, never supplied by the browser."""
    return f"user-{user.id}"


def me_payload(db: Session, user: User | None, settings: Settings) -> dict:
    shop = shop_of(db, user) if user else None
    return {
        "user": None if user is None else {
            "id": user.id, "name": user.name, "email": user.email,
            "paypal_verified": user.paypal_verified, "email_verified": user.email_verified, "is_dev": user.is_dev,
        },
        "shop": None if shop is None else {"id": shop.id, "name": shop.name, "verified": shop.verified},
        "dev_login": settings.dev_login,
    }


def _set_session_cookie(response, token: str, settings: Settings) -> None:
    response.set_cookie(
        SESSION_COOKIE, token, max_age=SESSION_DAYS * 86400, httponly=True,
        samesite="lax", secure=settings.cookie_secure, path="/",
    )


def safe_next(path: str | None) -> str:
    """Only ever send people back to a path on our own site, never to another address."""
    if path and path.startswith("/") and not path.startswith("//") and "\\" not in path and "\n" not in path:
        return path
    return "/"


# ---- routes ----------------------------------------------------------------

@router.get("/me")
def me(user: User | None = Depends(get_current_user), db: Session = Depends(get_session), settings: Settings = Depends(get_settings)):
    return me_payload(db, user, settings)


@router.get("/paypal/login")
def paypal_login(next: str = "/", paypal=Depends(get_paypal), settings: Settings = Depends(get_settings)):
    """Send the browser to PayPal. A random `state` guards against forged callbacks."""
    state = secrets.token_urlsafe(24)
    url = paypal.login_url(redirect_uri=f"{settings.public_api_url}/auth/paypal/callback", state=state)
    response = RedirectResponse(url, status_code=303)
    for name, value in ((STATE_COOKIE, state), (NEXT_COOKIE, safe_next(next))):
        response.set_cookie(name, value, max_age=STATE_MINUTES * 60, httponly=True, samesite="lax", secure=settings.cookie_secure, path="/")
    return response


@router.get("/paypal/callback")
def paypal_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    db: Session = Depends(get_session),
    paypal=Depends(get_paypal),
    settings: Settings = Depends(get_settings),
):
    expected, nxt = request.cookies.get(STATE_COOKIE), safe_next(request.cookies.get(NEXT_COOKIE))

    def back(reason: str | None = None) -> RedirectResponse:
        target = f"{settings.frontend_url}{nxt}" if reason is None else f"{settings.frontend_url}/signin?error={reason}"
        response = RedirectResponse(target, status_code=303)
        response.delete_cookie(STATE_COOKIE, path="/")
        response.delete_cookie(NEXT_COOKIE, path="/")
        return response

    if error or not code:
        return back("cancelled")
    if not expected or not state or not secrets.compare_digest(expected, state):
        return back("state")  # a forged or stale callback

    try:
        profile = paypal.get_login_profile(paypal.exchange_login_code(code))
    except PayPalError:
        return back("paypal")

    user = db.scalar(select(User).where(User.paypal_payer_id == profile["payer_id"]))
    if user is None:
        user = User(paypal_payer_id=profile["payer_id"])
        db.add(user)
    # Refresh from PayPal on every sign-in, so a newly verified account is noticed.
    user.email, user.email_verified = profile["email"], profile["email_verified"]
    user.name, user.paypal_verified = profile["name"], profile["verified_account"]
    db.commit()

    response = back()
    _set_session_cookie(response, start_session(db, user), settings)
    return response


class DevLoginIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    email: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$", max_length=254)
    verified: bool = True  # lets you try the "unverified seller" experience too
    email_verified: bool = True  # untick to try the email-code flow


@router.post("/dev-login")
def dev_login(body: DevLoginIn, db: Session = Depends(get_session), settings: Settings = Depends(get_settings)):
    """Local testing only. Answers 404, as if it did not exist, unless DEV_LOGIN=1."""
    if not settings.dev_login:
        raise HTTPException(404, "Not found.")
    user = db.scalar(select(User).where(User.is_dev.is_(True), User.email == body.email))
    if user is None:
        user = User(email=body.email, is_dev=True)
        db.add(user)
    user.name, user.email_verified, user.paypal_verified = body.name.strip(), body.email_verified, body.verified
    db.commit()
    response = JSONResponse(me_payload(db, user, settings))
    _set_session_cookie(response, start_session(db, user), settings)
    return response


@router.post("/logout")
def logout(request: Request, db: Session = Depends(get_session)):
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        db.execute(delete(AuthSession).where(AuthSession.token_hash == hash_token(token)))
        db.commit()
    response = JSONResponse({"ok": True})
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


# ---- email verification ------------------------------------------------------

def _code_hash(user_id: int, email: str, code: str) -> str:
    return hashlib.sha256(f"{user_id}:{email.lower()}:{code}".encode()).hexdigest()


def _mask(email: str) -> str:
    name, _, domain = email.partition("@")
    return f"{name[:1]}{'*' * max(len(name) - 1, 1)}@{domain}"


@router.post("/email/send")
def email_send(
    user: User = Depends(require_user),
    db: Session = Depends(get_session),
    mailer=Depends(get_mailer),
    settings: Settings = Depends(get_settings),
):
    """Email a 6-digit code to the address on the account."""
    if user.email_verified:
        return {"already_verified": True}

    now = _now()
    newest = db.scalar(select(EmailCode).where(EmailCode.user_id == user.id).order_by(EmailCode.id.desc()).limit(1))
    if newest and (now - newest.created_at).total_seconds() < RESEND_SECONDS:
        wait = RESEND_SECONDS - int((now - newest.created_at).total_seconds())
        raise HTTPException(429, f"Please wait {wait} seconds before asking for another code.")
    if not send_limiter.allow(user.id):
        raise HTTPException(429, "Too many codes requested. Please try again later.")

    code = f"{secrets.randbelow(1_000_000):06d}"
    for old in db.scalars(select(EmailCode).where(EmailCode.user_id == user.id, EmailCode.consumed_at.is_(None))):
        old.consumed_at = now  # only the newest code works
    db.add(EmailCode(user_id=user.id, email=user.email, code_hash=_code_hash(user.id, user.email, code),
                     expires_at=now + timedelta(minutes=CODE_MINUTES)))
    db.commit()

    if mailer is None:
        if settings.dev_login:  # local development only: show the code instead of emailing it
            return {"sent": False, "dev_code": code, "email": _mask(user.email)}
        raise HTTPException(503, "Email verification isn't set up yet.")
    try:
        mailer.send(
            user.email,
            "Your fullbatch verification code",
            f"Your fullbatch verification code is {code}.\n\nIt expires in {CODE_MINUTES} minutes. "
            "If you didn't ask for this, you can ignore this email.",
        )
    except MailError:
        raise HTTPException(502, "We couldn't send the email. Please try again in a moment.")
    return {"sent": True, "email": _mask(user.email)}


class EmailCodeIn(BaseModel):
    code: str = Field(pattern=r"^\d{6}$")


@router.post("/email/verify")
def email_verify(
    body: EmailCodeIn,
    user: User = Depends(require_user),
    db: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
):
    now = _now()
    row = db.scalar(
        select(EmailCode)
        .where(EmailCode.user_id == user.id, EmailCode.email == user.email, EmailCode.consumed_at.is_(None))
        .order_by(EmailCode.id.desc())
        .limit(1)
    )
    if row is None or row.expires_at <= now:
        raise HTTPException(400, "That code has expired. Please ask for a new one.")
    if row.attempts >= CODE_MAX_ATTEMPTS:
        raise HTTPException(429, "Too many wrong tries. Please ask for a new code.")

    if not secrets.compare_digest(row.code_hash, _code_hash(user.id, user.email, body.code)):
        row.attempts += 1
        db.commit()
        left = CODE_MAX_ATTEMPTS - row.attempts
        raise HTTPException(400, f"That code isn't right. {left} {'try' if left == 1 else 'tries'} left." if left else "Too many wrong tries. Please ask for a new code.")

    row.consumed_at = now
    user.email_verified = True
    db.commit()
    return me_payload(db, user, settings)
