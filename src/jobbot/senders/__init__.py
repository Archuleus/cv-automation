"""Delivery. Nothing here runs without an explicit human approval upstream."""

from jobbot.config import Settings, get_settings
from jobbot.senders.base import (
    Attachment,
    NotConfiguredError,
    Sender,
    SendError,
    SentMessage,
)
from jobbot.senders.limits import SendDecision, may_send, next_gap_seconds, sent_today
from jobbot.senders.oauth import AuthError, DeviceCode, DeviceCodeAuth, NotSignedInError, Token
from jobbot.senders.outlook import OutlookSender
from jobbot.senders.smtp import SmtpSender


def build_sender(settings: Settings | None = None) -> Sender:
    """The transport named by JOBBOT_MAIL_PROVIDER."""
    settings = settings or get_settings()
    if settings.mail_provider == "outlook":
        return OutlookSender(settings)
    return SmtpSender(settings)


__all__ = [
    "Attachment",
    "AuthError",
    "DeviceCode",
    "DeviceCodeAuth",
    "NotConfiguredError",
    "NotSignedInError",
    "OutlookSender",
    "SendDecision",
    "SendError",
    "Sender",
    "SentMessage",
    "SmtpSender",
    "Token",
    "build_sender",
    "may_send",
    "next_gap_seconds",
    "sent_today",
]
