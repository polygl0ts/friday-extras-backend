import logging
import smtplib
from email.message import EmailMessage

from app.config import settings

logger = logging.getLogger(__name__)


def invite_link(token: str) -> str:
    return f"{settings.web_origin.rstrip('/')}/lake-quals?token={token}"


def send_invite(team_name: str, email: str, token: str) -> None:
    """Email a team its invitation token. Run as a background task.

    Never raises: the token is already stored, and an admin can copy the link
    from the admin panel if the mail never arrives.
    """
    if not settings.smtp_host:
        logger.warning("SMTP not configured, invite for %s not emailed", team_name)
        return

    msg = EmailMessage()
    msg["From"] = settings.smtp_from or settings.smtp_user
    msg["To"] = email
    msg["Subject"] = "Lake CTF 26-27 Quals invitation"
    msg.set_content(
        f"""Hello {team_name}, we are happy to announce that you have been invited in Lake CTF 26-27 quals!

This means that you can now also invite teams to participate to the event.
Your invite token: {token}
Use it to invite other teams: {invite_link(token)}

We'd like to kindly remind you that this CTF is Human Only, be mindful :)

Looking forward to seeing you compete!
Polygl0ts
"""
    )
    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as smtp:
            smtp.starttls()
            smtp.login(settings.smtp_user, settings.smtp_password)
            smtp.send_message(msg)
    except (smtplib.SMTPException, OSError):
        logger.exception("Could not email the invite for %s", team_name)
