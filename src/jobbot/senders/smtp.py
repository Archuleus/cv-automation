"""Sending through SMTP with an app password.

Chosen because a personal Microsoft account often cannot create the Entra app
registration the OAuth path needs. The trade is real and worth stating plainly:
an app password grants POP, IMAP *and* SMTP, so unlike the `Mail.Send` OAuth
scope it can read the mailbox as well as send from it. It is still not the
account password, and it is revocable on its own.

Because the credential is broader, the handling here is narrower: the password
is never logged, never included in an exception message, and the connection is
refused outright if the server will not upgrade to TLS.
"""

from __future__ import annotations

import logging
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

from jobbot.config import Settings, get_settings
from jobbot.senders.base import Attachment, NotConfiguredError, SendError, SentMessage

logger = logging.getLogger("jobbot.senders.smtp")

# Ports that mean "upgrade this plaintext connection with STARTTLS". Anything
# else is treated as implicit TLS from the first byte.
STARTTLS_PORTS = frozenset({587, 25, 2525})


def build_email(
    *,
    sender: str,
    sender_name: str,
    to: str,
    subject: str,
    body: str,
    reply_to: str = "",
    attachments: tuple[Attachment, ...] = (),
) -> EmailMessage:
    """Assemble the message.

    Plain text, not HTML: an application is a letter, and HTML mail from an
    unfamiliar sender scores worse with spam filters than the same words as text.
    """
    message = EmailMessage()
    message["From"] = formataddr((sender_name, sender)) if sender_name else sender
    message["To"] = to
    message["Subject"] = subject
    # Providers rewrite or reject a From that is not the authenticated account,
    # so Reply-To is the only way to route answers to a different mailbox --
    # such as the address printed on the CV.
    if reply_to and reply_to != sender:
        message["Reply-To"] = reply_to
    # A well-formed Message-ID keyed to the sending domain is one of the cheapest
    # signals that a message came from a real mail client rather than a script.
    message["Message-ID"] = make_msgid(domain=sender.rsplit("@", 1)[-1])
    message.set_content(body)

    for item in attachments:
        message.add_attachment(
            item.content,
            maintype="application",
            subtype="pdf" if item.name.lower().endswith(".pdf") else "octet-stream",
            filename=item.name,
        )
    return message


class SmtpSender:
    """Sends one application at a time over an authenticated SMTP connection."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()

    def close(self) -> None:
        """No persistent connection is held; each send opens and closes its own."""

    def __enter__(self) -> SmtpSender:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # ------------------------------------------------------------------

    @property
    def sender_address(self) -> str:
        """The From address.

        This is the authenticated account, not the applicant's preferred
        address. Gmail and Outlook both rewrite or reject a From header that
        does not match the account that signed in, so claiming a different one
        would either be silently overwritten or bounce.
        """
        return self._settings.smtp_username or self._settings.applicant_email

    @property
    def reply_to_address(self) -> str:
        """Where replies go: the configured address, else the applicant's."""
        return self._settings.reply_to_email or self._settings.applicant_email

    def _require_config(self) -> None:
        missing = self._settings.missing_for("email")
        if missing:
            raise NotConfiguredError(
                "SMTP is not configured; missing " + ", ".join(missing) + ". "
                "See the mail setup section in README.md"
            )

    def _safe_detail(self, raw: bytes | str) -> str:
        """Server text, with the credential removed.

        Surfacing what the server said is genuinely useful for diagnosis, but
        the password must not ride along. Redacting here rather than trusting
        servers never to echo it keeps the guarantee independent of the server.
        """
        text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
        password = self._settings.smtp_password
        return text.replace(password, "***") if password else text

    def _connect(self) -> smtplib.SMTP:
        """Open an authenticated, encrypted connection, or fail."""
        self._require_config()
        settings = self._settings
        context = ssl.create_default_context()

        try:
            if settings.smtp_port in STARTTLS_PORTS:
                connection: smtplib.SMTP = smtplib.SMTP(
                    settings.smtp_host, settings.smtp_port, timeout=settings.smtp_timeout_seconds
                )
                connection.ehlo()
                connection.starttls(context=context)
                connection.ehlo()
            else:
                connection = smtplib.SMTP_SSL(
                    settings.smtp_host,
                    settings.smtp_port,
                    timeout=settings.smtp_timeout_seconds,
                    context=context,
                )
        except (OSError, smtplib.SMTPException) as error:
            raise SendError(
                f"could not reach {settings.smtp_host}:{settings.smtp_port} ({error})"
            ) from error

        try:
            connection.login(settings.smtp_username, settings.smtp_password)
        except smtplib.SMTPAuthenticationError as error:
            connection.close()
            detail = self._safe_detail(error.smtp_error)
            # Outlook.com answers a disabled-basic-auth account with 5.7.139.
            # That is a policy, not a typo: no password will ever work, so
            # saying "check your password" would send the reader in circles.
            if "basic authentication is disabled" in detail.lower() or "5.7.139" in detail:
                raise NotConfiguredError(
                    f"{settings.smtp_host} has disabled password sign-in for this "
                    "account, so no app password will work. The server still offers "
                    "XOAUTH2, which needs an OAuth token: set JOBBOT_MAIL_PROVIDER="
                    "outlook and register an app (see the mail section of README.md), "
                    "or send from a domain you control."
                ) from None
            raise NotConfiguredError(
                "the SMTP server rejected the credentials. Check that two-step "
                "verification is on and the app password was copied correctly. "
                f"(server said: {detail[:160]})"
            ) from None
        except smtplib.SMTPException as error:
            connection.close()
            raise SendError(f"SMTP login failed ({type(error).__name__})") from None

        return connection

    def health(self) -> str:
        """Verify the credentials by connecting and authenticating, sending nothing."""
        connection = self._connect()
        try:
            connection.noop()
        finally:
            with_suppressed_quit(connection)
        return f"ready to send as {self.sender_address} via {self._settings.smtp_host}"

    # ------------------------------------------------------------------

    def send(
        self,
        *,
        to: str,
        subject: str,
        body: str,
        attachments: tuple[Attachment, ...] = (),
    ) -> SentMessage:
        if not to.strip():
            raise SendError("no recipient address")
        if not subject.strip():
            raise SendError("refusing to send a message with no subject")
        if not body.strip():
            raise SendError("refusing to send an empty message")

        message = build_email(
            sender=self.sender_address,
            sender_name=self._settings.applicant_name,
            to=to,
            subject=subject,
            body=body,
            reply_to=self.reply_to_address,
            attachments=attachments,
        )

        connection = self._connect()
        try:
            connection.send_message(message)
        except smtplib.SMTPRecipientsRefused as error:
            raise SendError(f"the server refused the recipient {to}: {error.recipients}") from None
        except smtplib.SMTPDataError as error:
            # Providers throttle here rather than at login; the text is useful
            # and contains no credential.
            raise SendError(f"the server rejected the message ({error.smtp_code})") from None
        except smtplib.SMTPException as error:
            raise SendError(f"send failed ({type(error).__name__})") from None
        finally:
            with_suppressed_quit(connection)

        logger.info("sent to %s: %s", to, subject)
        return SentMessage(
            to=to,
            subject=subject,
            attachment_names=tuple(item.name for item in attachments),
            provider_id=str(message["Message-ID"]),
        )


def with_suppressed_quit(connection: smtplib.SMTP) -> None:
    """Close politely, but never let teardown mask a real send result."""
    try:
        connection.quit()
    except smtplib.SMTPException:
        connection.close()
