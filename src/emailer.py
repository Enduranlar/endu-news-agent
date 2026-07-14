"""SMTP email sender for the report.

Sends the inline-CSS HTML report (with a plain-text Markdown alternative) to
REPORT_TO. Goes DIRECT — never through the Turkish proxy. On send failure the
caller keeps the archived file, logs the error, and exits non-zero so cron mail
surfaces it; sent_at is recorded only on success.
"""

from __future__ import annotations

import logging
import smtplib
import ssl
from email.message import EmailMessage

from .settings import Settings

log = logging.getLogger("agent.emailer")


class EmailError(RuntimeError):
    """Raised when the report could not be sent."""


def send_report(
    settings: Settings,
    subject: str,
    html_body: str,
    text_body: str,
) -> None:
    if not settings.report_to:
        raise EmailError("REPORT_TO is empty; cannot send report")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = settings.report_from
    msg["To"] = ", ".join(settings.report_to)
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")

    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as server:
            server.ehlo()
            # STARTTLS on the submission port (587). Port 465 would use SMTP_SSL.
            if settings.smtp_port != 465:
                server.starttls(context=ssl.create_default_context())
                server.ehlo()
            if settings.smtp_user:
                server.login(settings.smtp_user, settings.smtp_pass)
            server.send_message(msg)
    except (smtplib.SMTPException, OSError) as exc:
        raise EmailError(f"SMTP send failed: {exc}") from exc

    log.info("report emailed to %s", ", ".join(settings.report_to))
