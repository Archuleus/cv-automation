"""Sending one application through Microsoft Graph.

The send itself is a single HTTP call. Everything around it exists because the
cost of a mistake is asymmetric: an application that fails to send can be retried
in a minute, while an address that gets filtered as bulk mail stops every future
application from arriving and cannot be undone.
"""

from __future__ import annotations

import logging

import httpx

from jobbot.config import Settings, get_settings
from jobbot.senders.base import Attachment, NotConfiguredError, SendError, SentMessage
from jobbot.senders.oauth import DeviceCodeAuth, NotSignedInError

logger = logging.getLogger("jobbot.senders.outlook")

SEND_MAIL_URL = "https://graph.microsoft.com/v1.0/me/sendMail"
ME_URL = "https://graph.microsoft.com/v1.0/me"


def as_graph_attachment(item: Attachment) -> dict[str, str]:
    return {
        "@odata.type": "#microsoft.graph.fileAttachment",
        "name": item.name,
        "contentBytes": item.base64_content,
    }


def build_message(
    *,
    to: str,
    subject: str,
    body: str,
    attachments: tuple[Attachment, ...] = (),
    reply_to: str = "",
) -> dict[str, object]:
    """The Graph message payload.

    Plain text, not HTML: an application is a letter, and HTML mail from an
    unknown sender scores worse with spam filters than the same words in text.
    """
    message: dict[str, object] = {
        "subject": subject,
        "body": {"contentType": "Text", "content": body},
        "toRecipients": [{"emailAddress": {"address": to}}],
    }
    if attachments:
        message["attachments"] = [as_graph_attachment(item) for item in attachments]
    if reply_to:
        message["replyTo"] = [{"emailAddress": {"address": reply_to}}]
    # Keeping a copy in Sent Items is the only record the applicant can check
    # independently of this tool's own database.
    return {"message": message, "saveToSentItems": True}


class OutlookSender:
    """Sends mail as the signed-in user, with Mail.Send and nothing more."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        auth: DeviceCodeAuth | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._auth = auth or DeviceCodeAuth(self._settings)
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=60.0)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> OutlookSender:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def health(self) -> str:
        """Confirm a usable token is stored, without sending anything."""
        if not self._settings.ms_client_id:
            raise NotConfiguredError(
                "JOBBOT_MS_CLIENT_ID is not set; see the mail setup in README.md"
            )
        token = self._auth.stored_token()
        if token is None:
            raise NotSignedInError("not signed in. Run 'jobbot mail login' first.")
        # Forces a refresh if the stored access token has expired, so a failure
        # surfaces here rather than on the first real application.
        self._auth.access_token()
        return f"ready to send as {token.account or 'the signed-in account'}"

    def whoami(self) -> str:
        """The mailbox the stored token actually sends from."""
        response = self._client.get(
            ME_URL, headers={"Authorization": f"Bearer {self._auth.access_token()}"}
        )
        if response.status_code == 403:
            # Mail.Send alone does not grant profile reads. Not being able to
            # answer this question is the scope working as intended.
            token = self._auth.stored_token()
            return token.account if token and token.account else "unknown"
        if response.status_code != 200:
            raise SendError(f"could not read the account: {response.text[:200]}")
        payload = response.json()
        return str(payload.get("userPrincipalName") or payload.get("mail") or "unknown")

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

        payload = build_message(
            to=to,
            subject=subject,
            body=body,
            attachments=attachments,
            reply_to=self._settings.applicant_email,
        )
        response = self._client.post(
            SEND_MAIL_URL,
            headers={
                "Authorization": f"Bearer {self._auth.access_token()}",
                "Content-Type": "application/json",
            },
            json=payload,
        )

        # Graph answers a successful sendMail with 202 Accepted and no body.
        if response.status_code == 202:
            logger.info("sent to %s: %s", to, subject)
            return SentMessage(
                to=to, subject=subject, attachment_names=tuple(a.name for a in attachments)
            )

        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After", "unknown")
            raise SendError(f"throttled by Microsoft; retry after {retry_after}s")
        if response.status_code in (401, 403):
            raise SendError(
                "the stored token was rejected. Run 'jobbot mail login' again "
                f"({response.status_code})"
            )
        raise SendError(f"send failed ({response.status_code}): {response.text[:300]}")
