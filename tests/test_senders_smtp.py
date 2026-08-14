"""The SMTP transport, driven by a fake server object.

Nothing here opens a socket. What matters is the message that would go on the
wire and the way credentials are handled when things fail.
"""

from __future__ import annotations

import smtplib
from email.message import EmailMessage

import pytest

from jobbot.config import Settings
from jobbot.senders import build_sender
from jobbot.senders.base import Attachment, NotConfiguredError, SendError
from jobbot.senders.outlook import OutlookSender
from jobbot.senders.smtp import STARTTLS_PORTS, SmtpSender, build_email

PASSWORD = "app-password-16ch"


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        mail_provider="smtp",
        smtp_host="smtp-mail.outlook.com",
        smtp_port=587,
        smtp_username="applicant@hotmail.com",
        smtp_password=PASSWORD,
        applicant_email="applicant@hotmail.com",
        applicant_name="Test Applicant",
    )


class FakeSmtp:
    """Records what a real smtplib.SMTP would have been asked to do."""

    def __init__(self, *, login_error: Exception | None = None,
                 send_error: Exception | None = None) -> None:
        self.login_error = login_error
        self.send_error = send_error
        self.started_tls = False
        self.logged_in_as: str | None = None
        self.sent: list[EmailMessage] = []
        self.quit_called = False

    def ehlo(self) -> None: ...

    def starttls(self, context: object = None) -> None:
        self.started_tls = True

    def login(self, username: str, password: str) -> None:
        if self.login_error:
            raise self.login_error
        self.logged_in_as = username

    def noop(self) -> None: ...

    def send_message(self, message: EmailMessage) -> None:
        if self.send_error:
            raise self.send_error
        self.sent.append(message)

    def quit(self) -> None:
        self.quit_called = True

    def close(self) -> None: ...


@pytest.fixture
def fake(monkeypatch) -> FakeSmtp:
    server = FakeSmtp()
    monkeypatch.setattr("smtplib.SMTP", lambda *a, **k: server)
    monkeypatch.setattr("smtplib.SMTP_SSL", lambda *a, **k: server)
    return server


class TestBuildEmail:
    def test_sends_plain_text_not_html(self):
        message = build_email(
            sender="a@b.com", sender_name="A", to="hr@acme.com", subject="s", body="b"
        )

        assert message.get_content_type() == "text/plain"

    def test_sets_a_display_name(self):
        message = build_email(
            sender="a@b.com", sender_name="Zeynep Çağlar", to="x@y.com", subject="s", body="b"
        )

        assert "Zeynep" in str(message["From"])
        assert "a@b.com" in str(message["From"])

    def test_falls_back_to_a_bare_address(self):
        message = build_email(
            sender="a@b.com", sender_name="", to="x@y.com", subject="s", body="b"
        )

        assert message["From"] == "a@b.com"

    def test_message_id_uses_the_sending_domain(self):
        # A well-formed Message-ID is a cheap signal of a real mail client.
        message = build_email(
            sender="a@hotmail.com", sender_name="", to="x@y.com", subject="s", body="b"
        )

        assert str(message["Message-ID"]).endswith("@hotmail.com>")

    def test_attaches_a_pdf_with_the_right_type(self, tmp_path):
        cv = tmp_path / "cv.pdf"
        cv.write_bytes(b"%PDF-1.7 fake")

        message = build_email(
            sender="a@b.com",
            sender_name="",
            to="x@y.com",
            subject="s",
            body="b",
            attachments=(Attachment.from_path(cv),),
        )

        attachment = next(iter(message.iter_attachments()))
        assert attachment.get_filename() == "cv.pdf"
        assert attachment.get_content_type() == "application/pdf"


class TestAddressing:
    def test_from_is_the_authenticated_account_not_the_preferred_address(
        self, settings: Settings
    ):
        # Gmail and Outlook rewrite or reject a From that is not the signed-in
        # account, so claiming the CV address here would silently not work.
        mixed = settings.model_copy(
            update={
                "smtp_username": "sender@gmail.com",
                "applicant_email": "cv-address@hotmail.com",
            }
        )

        assert SmtpSender(mixed).sender_address == "sender@gmail.com"

    def test_replies_go_to_the_applicants_address(self, settings: Settings):
        mixed = settings.model_copy(
            update={
                "smtp_username": "sender@gmail.com",
                "applicant_email": "cv-address@hotmail.com",
            }
        )

        assert SmtpSender(mixed).reply_to_address == "cv-address@hotmail.com"

    def test_an_explicit_reply_to_wins(self, settings: Settings):
        mixed = settings.model_copy(
            update={
                "applicant_email": "cv-address@hotmail.com",
                "reply_to_email": "elsewhere@example.com",
            }
        )

        assert SmtpSender(mixed).reply_to_address == "elsewhere@example.com"

    def test_reply_to_header_is_written(self, settings: Settings, fake: FakeSmtp):
        mixed = settings.model_copy(
            update={
                "smtp_username": "sender@gmail.com",
                "applicant_email": "cv-address@hotmail.com",
            }
        )

        SmtpSender(mixed).send(to="hr@acme.com", subject="s", body="b")

        assert fake.sent[0]["Reply-To"] == "cv-address@hotmail.com"

    def test_no_reply_to_header_when_it_matches_the_sender(
        self, settings: Settings, fake: FakeSmtp
    ):
        # A Reply-To identical to From is noise that some filters score against.
        SmtpSender(settings).send(to="hr@acme.com", subject="s", body="b")

        assert fake.sent[0]["Reply-To"] is None


class TestConnection:
    def test_upgrades_to_tls_on_a_starttls_port(self, settings: Settings, fake: FakeSmtp):
        SmtpSender(settings).health()

        assert fake.started_tls is True
        assert fake.logged_in_as == "applicant@hotmail.com"

    def test_uses_implicit_tls_on_other_ports(self, settings: Settings, fake: FakeSmtp):
        implicit = settings.model_copy(update={"smtp_port": 465})

        SmtpSender(implicit).health()

        # SMTP_SSL is already encrypted, so no STARTTLS upgrade is issued.
        assert fake.started_tls is False

    def test_587_is_a_starttls_port(self):
        assert 587 in STARTTLS_PORTS

    def test_refuses_to_run_unconfigured(self, tmp_path):
        blank = Settings(_env_file=None, mail_provider="smtp", smtp_password="")

        with pytest.raises(NotConfiguredError, match="smtp_password"):
            SmtpSender(blank).health()

    def test_a_wrong_password_says_to_check_the_password(
        self, settings: Settings, monkeypatch
    ):
        server = FakeSmtp(
            login_error=smtplib.SMTPAuthenticationError(535, b"5.7.3 Authentication failure")
        )
        monkeypatch.setattr("smtplib.SMTP", lambda *a, **k: server)

        with pytest.raises(NotConfiguredError, match="app password was copied correctly"):
            SmtpSender(settings).health()

    def test_disabled_basic_auth_does_not_send_the_reader_after_the_password(
        self, settings: Settings, monkeypatch
    ):
        # Outlook.com answers a disabled account with exactly this. It is a
        # policy, not a typo -- no password will ever work, so telling the user
        # to re-check theirs would send them in circles. Verbatim from a live run.
        server = FakeSmtp(
            login_error=smtplib.SMTPAuthenticationError(
                535,
                b"5.7.139 Authentication unsuccessful, basic authentication is disabled. "
                b"[FR4P281CA0026.DEUP281.PROD.OUTLOOK.COM]",
            )
        )
        monkeypatch.setattr("smtplib.SMTP", lambda *a, **k: server)

        with pytest.raises(NotConfiguredError) as excinfo:
            SmtpSender(settings).health()

        message = str(excinfo.value)
        assert "no app password will work" in message
        assert "XOAUTH2" in message
        assert "copied correctly" not in message

    def test_the_password_never_appears_in_an_error(self, settings: Settings, monkeypatch):
        server = FakeSmtp(login_error=smtplib.SMTPAuthenticationError(535, PASSWORD.encode()))
        monkeypatch.setattr("smtplib.SMTP", lambda *a, **k: server)

        with pytest.raises(NotConfiguredError) as excinfo:
            SmtpSender(settings).health()

        assert PASSWORD not in str(excinfo.value)

    def test_an_unreachable_server_is_reported_clearly(self, settings: Settings, monkeypatch):
        monkeypatch.setattr(
            "smtplib.SMTP", lambda *a, **k: (_ for _ in ()).throw(OSError("no route"))
        )

        with pytest.raises(SendError, match="could not reach"):
            SmtpSender(settings).health()


class TestSend:
    def test_sends_and_reports_the_message_id(self, settings: Settings, fake: FakeSmtp):
        result = SmtpSender(settings).send(
            to="hr@acme.com", subject="Backend Developer", body="Hello."
        )

        assert result.to == "hr@acme.com"
        assert result.provider_id.endswith("@hotmail.com>")
        assert len(fake.sent) == 1

    def test_closes_the_connection_afterwards(self, settings: Settings, fake: FakeSmtp):
        SmtpSender(settings).send(to="hr@acme.com", subject="s", body="b")

        assert fake.quit_called is True

    def test_attaches_the_cv(self, settings: Settings, fake: FakeSmtp, tmp_path):
        cv = tmp_path / "Yuksel_CV.pdf"
        cv.write_bytes(b"%PDF-1.7 fake")

        result = SmtpSender(settings).send(
            to="hr@acme.com",
            subject="s",
            body="b",
            attachments=(Attachment.from_path(cv),),
        )

        assert result.attachment_names == ("Yuksel_CV.pdf",)
        assert len(list(fake.sent[0].iter_attachments())) == 1

    @pytest.mark.parametrize(("field", "value"), [("to", " "), ("subject", " "), ("body", " ")])
    def test_refuses_an_incomplete_message(
        self, settings: Settings, fake: FakeSmtp, field: str, value: str
    ):
        payload = {"to": "hr@acme.com", "subject": "s", "body": "b"}
        payload[field] = value

        with pytest.raises(SendError):
            SmtpSender(settings).send(**payload)  # type: ignore[arg-type]
        assert fake.sent == []

    def test_a_refused_recipient_names_the_address(self, settings: Settings, monkeypatch):
        server = FakeSmtp(send_error=smtplib.SMTPRecipientsRefused({"hr@acme.com": (550, b"no")}))
        monkeypatch.setattr("smtplib.SMTP", lambda *a, **k: server)

        with pytest.raises(SendError, match=r"hr@acme\.com"):
            SmtpSender(settings).send(to="hr@acme.com", subject="s", body="b")

    def test_a_rejected_message_reports_the_code(self, settings: Settings, monkeypatch):
        server = FakeSmtp(send_error=smtplib.SMTPDataError(554, b"throttled"))
        monkeypatch.setattr("smtplib.SMTP", lambda *a, **k: server)

        with pytest.raises(SendError, match="554"):
            SmtpSender(settings).send(to="hr@acme.com", subject="s", body="b")

    def test_the_connection_closes_even_when_the_send_fails(
        self, settings: Settings, monkeypatch
    ):
        server = FakeSmtp(send_error=smtplib.SMTPDataError(554, b"nope"))
        monkeypatch.setattr("smtplib.SMTP", lambda *a, **k: server)

        with pytest.raises(SendError):
            SmtpSender(settings).send(to="hr@acme.com", subject="s", body="b")

        assert server.quit_called is True


class TestProviderSelection:
    def test_smtp_is_the_default(self, settings: Settings):
        assert isinstance(build_sender(settings), SmtpSender)

    def test_outlook_is_selectable(self, settings: Settings):
        assert isinstance(
            build_sender(settings.model_copy(update={"mail_provider": "outlook"})), OutlookSender
        )

    def test_requirements_follow_the_selected_provider(self, settings: Settings):
        # `config check` should report what the *selected* transport needs, not
        # demand settings for one that is not in use.
        assert "smtp_password" in settings.required_for("email")
        assert "ms_client_id" not in settings.required_for("email")

        outlook = settings.model_copy(update={"mail_provider": "outlook"})
        assert "ms_client_id" in outlook.required_for("email")
