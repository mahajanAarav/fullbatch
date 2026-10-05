"""Sending email over plain SMTP, so any free provider works (Gmail app password, Brevo, and so on)."""

import smtplib
import ssl
from email.message import EmailMessage

from app.config import Settings


class MailError(Exception):
    """The email could not be sent."""


class SmtpMailer:
    def __init__(self, host: str, port: int, user: str, password: str, sender: str, timeout: float = 10.0):
        self._host, self._port, self._user, self._password = host, port, user, password
        self._sender, self._timeout = sender, timeout

    def send(self, to: str, subject: str, body: str) -> None:
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = self._sender, to, subject
        msg.set_content(body)
        try:
            if self._port == 465:  # implicit TLS
                server = smtplib.SMTP_SSL(self._host, self._port, timeout=self._timeout, context=ssl.create_default_context())
            else:  # STARTTLS, the usual choice on 587
                server = smtplib.SMTP(self._host, self._port, timeout=self._timeout)
                server.starttls(context=ssl.create_default_context())
            with server:
                if self._user:
                    server.login(self._user, self._password)
                server.send_message(msg)
        except (smtplib.SMTPException, OSError) as err:
            raise MailError(f"{type(err).__name__}") from err  # never put the provider's message (it can echo credentials) in logs or replies


def from_settings(settings: Settings) -> SmtpMailer | None:
    """A mailer if email is configured, else None (the app then says verification is not set up)."""
    if not settings.smtp_host or not settings.email_from:
        return None
    return SmtpMailer(settings.smtp_host, settings.smtp_port, settings.smtp_user, settings.smtp_password, settings.email_from)
