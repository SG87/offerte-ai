"""Outbox: every mail is stored first, then sent over SMTP if a mail server is configured."""

from __future__ import annotations

import smtplib
from datetime import UTC, datetime
from email.message import EmailMessage
from email.utils import formataddr

from sqlmodel import Session

from app.config import get_settings
from app.models import Organisation, OutboxMessage, Project, Quote


def queue_mail(
    session: Session,
    org: Organisation,
    to: str,
    subject: str,
    body: str,
    kind: str,
    project_id: int | None = None,
    quote_id: int | None = None,
    order_id: int | None = None,
    invoice_id: int | None = None,
) -> OutboxMessage:
    msg = OutboxMessage(
        org_id=org.id, project_id=project_id, quote_id=quote_id, order_id=order_id, invoice_id=invoice_id, kind=kind,
        to=to, subject=subject, body=body,
    )
    session.add(msg)
    session.commit()
    session.refresh(msg)
    deliver(session, org, msg)
    return msg


def _quote(session: Session, org: Organisation, msg: OutboxMessage) -> tuple[Project, Quote] | None:
    quote = session.get(Quote, msg.quote_id) if msg.quote_id else None
    project = session.get(Project, quote.project_id) if quote else None
    return (project, quote) if project and project.org_id == org.id else None


def attachments(session: Session, org: Organisation, msg: OutboxMessage) -> list[tuple[str, str, bytes]]:
    """(filename, mime type, content). Attachments are not stored: they are rebuilt from the quote when sending,
    so a mail that waited in the outbox still goes out with its PDF."""
    if msg.invoice_id:
        from app import invoicing

        if inv := invoicing.get(session, org, msg.invoice_id):
            return [(f"factuur-{inv.number or 'concept'}.pdf", "application/pdf", invoicing.invoice_pdf(session, org, inv))]
        return []
    if not (pq := _quote(session, org, msg)):
        return []
    from app import services  # services imports the workflow, which imports this module

    project, quote = pq
    return [(f"offerte-{quote.number}-v{quote.version}.pdf", "application/pdf", services.offerte_pdf(session, org, project, quote))]


def content(session: Session, org: Organisation, msg: OutboxMessage, banner: str | None = None) -> tuple[str, str | None]:
    """(plain text, html or None). A quote mail gets the accept/decline buttons, an order mail the supplier's
    confirm button."""
    prefix = banner + "\n" + "-" * 60 + "\n\n" if banner else ""
    if msg.order_id:
        from app import workflow
        from app.outputs import order_mail

        if m := workflow.order_mail(session, org, msg.order_id, msg.subject, msg.body):
            return prefix + order_mail.build_text(m), order_mail.build_html(m, banner)
    if not (pq := _quote(session, org, msg)):
        return prefix + msg.body, None
    from app import workflow
    from app.outputs.quote_mail import build_html, build_text

    m = workflow.quote_mail(session, org, pq[0], pq[1], msg.subject, msg.body)
    return prefix + build_text(m), build_html(m, banner)


def deliver(session: Session, org: Organisation, msg: OutboxMessage) -> None:
    """Try to send; without a ready SMTP server the message stays 'wacht' so it can be read (and sent) later.
    In test mode (OFFERTE_MAIL_REDIRECT_TO) it goes to that address, marked with the intended recipient."""
    settings = get_settings()
    if not settings.smtp_ready:
        return
    to, subject, banner = msg.to, msg.subject, None
    if settings.mail_redirect_to:
        to = settings.mail_redirect_to
        subject = f"[TEST → {msg.to}] {msg.subject}"
        banner = (
            f"TESTMODUS — deze mail was bedoeld voor {msg.to}. "
            "Zet OFFERTE_MAIL_REDIRECT_TO leeg in .env om mails echt naar de ontvangers te sturen."
        )
    email = EmailMessage()
    email["From"] = formataddr((settings.mail_from_name, settings.smtp_from or org.email))
    email["To"] = to
    email["Reply-To"] = settings.smtp_from if settings.mail_redirect_to and settings.smtp_from else org.email
    email["Subject"] = subject
    try:
        text, html = content(session, org, msg, banner)
        email.set_content(text)
        if html:
            email.add_alternative(html, subtype="html")
        for filename, mime, data in attachments(session, org, msg):
            maintype, subtype = mime.split("/", 1)
            email.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)
        # 465 = implicit TLS (SMTPS); anything else (587) upgrades with STARTTLS
        smtp_cls = smtplib.SMTP_SSL if settings.smtp_port == 465 else smtplib.SMTP
        with smtp_cls(settings.smtp_host, settings.smtp_port, timeout=10) as smtp:
            if smtp_cls is smtplib.SMTP:
                smtp.starttls()
            if settings.smtp_user:
                smtp.login(settings.smtp_user, settings.smtp_password or "")
            smtp.send_message(email)
        msg.status, msg.sent_at, msg.sent_to, msg.error = "verzonden", datetime.now(UTC), to, None
    except Exception as exc:  # SMTP/network errors, or the PDF could not be built: keep it in the outbox
        msg.status, msg.error = "mislukt", f"{type(exc).__name__}: {exc}"
    session.add(msg)
    session.commit()
