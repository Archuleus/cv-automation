"""OAuth token handling and the Graph send call, against recorded responses."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx

from jobbot.config import Settings
from jobbot.senders.base import Attachment, SendError
from jobbot.senders.oauth import (
    DEVICE_CODE_URL,
    SCOPES,
    TOKEN_URL,
    AuthError,
    DeviceCodeAuth,
    NotSignedInError,
    Token,
)
from jobbot.senders.outlook import SEND_MAIL_URL, OutlookSender, build_message


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        ms_client_id="test-client-id",
        ms_token_path=str(tmp_path / "token.json"),
        applicant_email="applicant@example.com",
    )


@pytest.fixture
def auth(settings: Settings) -> DeviceCodeAuth:
    return DeviceCodeAuth(settings, client=httpx.Client())


def _token(**overrides) -> Token:
    payload = {
        "access_token": "at-1",
        "refresh_token": "rt-1",
        "expires_at": datetime.now(UTC) + timedelta(hours=1),
        "account": "applicant@example.com",
    }
    payload.update(overrides)
    return Token(**payload)  # type: ignore[arg-type]


class TestScope:
    def test_requests_send_permission_and_nothing_else(self):
        # The whole safety argument rests on this: the token can send mail and
        # cannot read the mailbox, list contacts, or delete anything.
        assert "https://graph.microsoft.com/Mail.Send" in SCOPES
        assert not any("Read" in scope or "ReadWrite" in scope for scope in SCOPES)


class TestTokenStorage:
    def test_round_trips_through_disk(self, auth: DeviceCodeAuth):
        original = _token()

        auth._save(original)
        restored = auth.stored_token()

        assert restored is not None
        assert restored.access_token == original.access_token
        assert restored.refresh_token == original.refresh_token

    def test_no_token_before_sign_in(self, auth: DeviceCodeAuth):
        assert auth.stored_token() is None

    def test_a_corrupt_file_reads_as_no_token(self, auth: DeviceCodeAuth):
        auth.token_path.parent.mkdir(parents=True, exist_ok=True)
        auth.token_path.write_text("{not json", encoding="utf-8")

        assert auth.stored_token() is None

    def test_forget_deletes_the_file(self, auth: DeviceCodeAuth):
        auth._save(_token())

        assert auth.forget() is True
        assert auth.stored_token() is None

    def test_forget_is_harmless_when_nothing_is_stored(self, auth: DeviceCodeAuth):
        assert auth.forget() is False

    def test_an_expiring_token_is_not_fresh(self):
        # Refreshed early, so a token cannot expire mid-send.
        soon = _token(expires_at=datetime.now(UTC) + timedelta(minutes=2))

        assert not soon.is_fresh


class TestSignIn:
    def test_missing_client_id_explains_the_setup(self, tmp_path):
        blank = Settings(_env_file=None, ms_client_id="", ms_token_path=str(tmp_path / "t.json"))

        with pytest.raises(AuthError, match=r"entra\.microsoft\.com"):
            DeviceCodeAuth(blank, client=httpx.Client()).begin_sign_in()

    @respx.mock
    def test_begin_returns_the_code_the_user_must_type(self, auth: DeviceCodeAuth):
        respx.post(DEVICE_CODE_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "user_code": "ABCD-EFGH",
                    "verification_uri": "https://microsoft.com/devicelogin",
                    "device_code": "dev-1",
                    "interval": 1,
                    "expires_in": 900,
                },
            )
        )

        code = auth.begin_sign_in()

        assert code.user_code == "ABCD-EFGH"
        assert code.verification_uri.endswith("devicelogin")


class TestAccessToken:
    def test_refuses_when_not_signed_in(self, auth: DeviceCodeAuth):
        with pytest.raises(NotSignedInError, match="jobbot mail login"):
            auth.access_token()

    def test_returns_a_fresh_token_without_a_network_call(self, auth: DeviceCodeAuth):
        auth._save(_token())

        assert auth.access_token() == "at-1"

    @respx.mock
    def test_refreshes_a_stale_token_silently(self, auth: DeviceCodeAuth):
        auth._save(_token(expires_at=datetime.now(UTC) - timedelta(minutes=1)))
        respx.post(TOKEN_URL).mock(
            return_value=httpx.Response(
                200, json={"access_token": "at-2", "refresh_token": "rt-2", "expires_in": 3600}
            )
        )

        assert auth.access_token() == "at-2"
        stored = auth.stored_token()
        assert stored is not None and stored.refresh_token == "rt-2"

    @respx.mock
    def test_keeps_the_old_refresh_token_when_none_is_returned(self, auth: DeviceCodeAuth):
        auth._save(_token(expires_at=datetime.now(UTC) - timedelta(minutes=1)))
        respx.post(TOKEN_URL).mock(
            return_value=httpx.Response(200, json={"access_token": "at-2", "expires_in": 3600})
        )

        auth.access_token()

        stored = auth.stored_token()
        assert stored is not None and stored.refresh_token == "rt-1"

    @respx.mock
    def test_a_rejected_refresh_asks_for_a_new_sign_in(self, auth: DeviceCodeAuth):
        auth._save(_token(expires_at=datetime.now(UTC) - timedelta(minutes=1)))
        respx.post(TOKEN_URL).mock(return_value=httpx.Response(400, json={"error": "invalid"}))

        with pytest.raises(NotSignedInError, match="mail login"):
            auth.access_token()


class TestBuildMessage:
    def test_sends_plain_text_not_html(self):
        # HTML mail from an unknown sender scores worse with spam filters than
        # the same words as text.
        payload = build_message(to="hr@acme.com", subject="s", body="b")

        assert payload["message"]["body"]["contentType"] == "Text"  # type: ignore[index]

    def test_keeps_a_copy_in_sent_items(self):
        # The only record the applicant can check independently of this tool.
        assert build_message(to="a@b.com", subject="s", body="b")["saveToSentItems"] is True

    def test_attaches_the_cv_as_base64(self, tmp_path):
        cv = tmp_path / "cv.pdf"
        cv.write_bytes(b"%PDF-1.7 fake")

        payload = build_message(
            to="a@b.com", subject="s", body="b", attachments=(Attachment.from_path(cv),)
        )

        attachment = payload["message"]["attachments"][0]  # type: ignore[index]
        assert attachment["name"] == "cv.pdf"
        assert attachment["@odata.type"] == "#microsoft.graph.fileAttachment"

    def test_rejects_a_missing_attachment(self, tmp_path):
        with pytest.raises(SendError, match="not found"):
            Attachment.from_path(tmp_path / "absent.pdf")

    def test_rejects_an_oversized_attachment(self, tmp_path):
        big = tmp_path / "big.pdf"
        big.write_bytes(b"x" * (4 * 1024 * 1024))

        with pytest.raises(SendError, match="limit is"):
            Attachment.from_path(big)


class TestSend:
    @pytest.fixture
    def sender(self, settings: Settings, auth: DeviceCodeAuth) -> OutlookSender:
        auth._save(_token())
        return OutlookSender(settings, auth=auth, client=httpx.Client())

    @respx.mock
    def test_accepts_a_202_as_success(self, sender: OutlookSender):
        respx.post(SEND_MAIL_URL).mock(return_value=httpx.Response(202))

        result = sender.send(to="hr@acme.com", subject="Backend Developer", body="Hello.")

        assert result.to == "hr@acme.com"

    @respx.mock
    def test_sets_reply_to_the_applicant(self, sender: OutlookSender):
        route = respx.post(SEND_MAIL_URL).mock(return_value=httpx.Response(202))

        sender.send(to="hr@acme.com", subject="s", body="b")

        import json

        sent = json.loads(route.calls[0].request.content)
        assert sent["message"]["replyTo"][0]["emailAddress"]["address"] == "applicant@example.com"

    @pytest.mark.parametrize(
        ("field", "value"),
        [("to", "  "), ("subject", "  "), ("body", "  ")],
    )
    def test_refuses_to_send_an_incomplete_message(
        self, sender: OutlookSender, field: str, value: str
    ):
        payload = {"to": "hr@acme.com", "subject": "s", "body": "b"}
        payload[field] = value

        with pytest.raises(SendError):
            sender.send(**payload)  # type: ignore[arg-type]

    @respx.mock
    def test_throttling_reports_the_retry_delay(self, sender: OutlookSender):
        respx.post(SEND_MAIL_URL).mock(
            return_value=httpx.Response(429, headers={"Retry-After": "120"})
        )

        with pytest.raises(SendError, match="retry after 120s"):
            sender.send(to="hr@acme.com", subject="s", body="b")

    @respx.mock
    @pytest.mark.parametrize("status", [401, 403])
    def test_a_rejected_token_asks_for_a_new_sign_in(self, sender: OutlookSender, status: int):
        respx.post(SEND_MAIL_URL).mock(return_value=httpx.Response(status, json={}))

        with pytest.raises(SendError, match="mail login"):
            sender.send(to="hr@acme.com", subject="s", body="b")

    @respx.mock
    def test_an_unexpected_status_is_reported_verbatim(self, sender: OutlookSender):
        respx.post(SEND_MAIL_URL).mock(return_value=httpx.Response(500, text="boom"))

        with pytest.raises(SendError, match="500"):
            sender.send(to="hr@acme.com", subject="s", body="b")
