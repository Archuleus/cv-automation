"""Microsoft identity device-code flow.

Chosen over a redirect flow because this is a command-line tool: device code
needs no local web server and no registered redirect URI. The user opens a page,
types a short code, and consents in the browser -- the password is entered on
Microsoft's own page and never passes through this process.

What is stored afterwards is a refresh token scoped to `Mail.Send`. It can send
mail as the user and can do nothing else: it cannot read the mailbox, list
contacts, or delete anything, and the user can revoke it from their Microsoft
account page without changing their password.
"""

from __future__ import annotations

import contextlib
import json
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from jobbot.config import Settings, get_settings

logger = logging.getLogger("jobbot.senders.oauth")

# "consumers" is the personal-account authority: outlook.com, hotmail.com, live.com.
AUTHORITY = "https://login.microsoftonline.com/consumers/oauth2/v2.0"
DEVICE_CODE_URL = f"{AUTHORITY}/devicecode"
TOKEN_URL = f"{AUTHORITY}/token"

# Send only. Deliberately not Mail.ReadWrite, not Mail.Read, not User.Read.
# offline_access is what makes a refresh token possible, so consent is once.
SCOPES = ("https://graph.microsoft.com/Mail.Send", "offline_access")

# Refresh a little early: a token that expires mid-send fails the send.
EXPIRY_MARGIN = timedelta(minutes=5)


class AuthError(RuntimeError):
    """Sign-in failed, or no usable token is stored."""


class NotSignedInError(AuthError):
    """No token on disk; the user has not completed sign-in yet."""


@dataclass(frozen=True, slots=True)
class DeviceCode:
    """What the user needs in order to consent, in a browser, elsewhere."""

    user_code: str
    verification_uri: str
    device_code: str
    interval: int
    expires_in: int
    message: str


@dataclass(frozen=True, slots=True)
class Token:
    access_token: str
    refresh_token: str
    expires_at: datetime
    account: str = ""

    @property
    def is_fresh(self) -> bool:
        return datetime.now(UTC) + EXPIRY_MARGIN < self.expires_at

    def to_json(self) -> str:
        return json.dumps(
            {
                "access_token": self.access_token,
                "refresh_token": self.refresh_token,
                "expires_at": self.expires_at.isoformat(),
                "account": self.account,
            },
            indent=2,
        )

    @classmethod
    def from_json(cls, raw: str) -> Token:
        data = json.loads(raw)
        return cls(
            access_token=str(data["access_token"]),
            refresh_token=str(data["refresh_token"]),
            expires_at=datetime.fromisoformat(str(data["expires_at"])),
            account=str(data.get("account", "")),
        )


def _expires_at(payload: dict[str, Any]) -> datetime:
    raw = payload.get("expires_in")
    seconds = int(raw) if isinstance(raw, int | str) else 3600
    return datetime.now(UTC) + timedelta(seconds=seconds)


class DeviceCodeAuth:
    """Acquires and refreshes a Mail.Send token."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        client: httpx.Client | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._client_id = self._settings.ms_client_id
        self._token_path = self._settings.ms_token_file
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=30.0)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> DeviceCodeAuth:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Storage
    # ------------------------------------------------------------------

    @property
    def token_path(self) -> Path:
        return self._token_path

    def stored_token(self) -> Token | None:
        if not self._token_path.exists():
            return None
        try:
            return Token.from_json(self._token_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, KeyError, ValueError) as error:
            logger.warning("stored token at %s is unreadable: %s", self._token_path, error)
            return None

    def _save(self, token: Token) -> None:
        self._token_path.parent.mkdir(parents=True, exist_ok=True)
        self._token_path.write_text(token.to_json(), encoding="utf-8")
        # Owner-only. Best effort: Windows ignores POSIX mode bits.
        with contextlib.suppress(OSError):
            self._token_path.chmod(0o600)

    def forget(self) -> bool:
        """Delete the stored token. Revoking access is a separate, server-side act."""
        if not self._token_path.exists():
            return False
        self._token_path.unlink()
        return True

    # ------------------------------------------------------------------
    # Sign-in
    # ------------------------------------------------------------------

    def _require_client_id(self) -> None:
        if not self._client_id:
            raise AuthError(
                "JOBBOT_MS_CLIENT_ID is not set. Register a free application at "
                "https://entra.microsoft.com -> App registrations -> New registration, "
                "choose 'Personal Microsoft accounts only', enable 'Allow public client "
                "flows', and copy the Application (client) ID."
            )

    def begin_sign_in(self) -> DeviceCode:
        self._require_client_id()
        response = self._client.post(
            DEVICE_CODE_URL,
            data={"client_id": self._client_id, "scope": " ".join(SCOPES)},
        )
        if response.status_code != 200:
            raise AuthError(f"could not start sign-in: {response.text[:300]}")
        payload = response.json()
        return DeviceCode(
            user_code=str(payload["user_code"]),
            verification_uri=str(payload["verification_uri"]),
            device_code=str(payload["device_code"]),
            interval=int(payload.get("interval", 5)),
            expires_in=int(payload.get("expires_in", 900)),
            message=str(payload.get("message", "")),
        )

    def complete_sign_in(self, code: DeviceCode, *, account: str = "") -> Token:
        """Poll until the user consents in the browser, or the code expires."""
        deadline = time.monotonic() + code.expires_in
        interval = code.interval

        while time.monotonic() < deadline:
            time.sleep(interval)
            response = self._client.post(
                TOKEN_URL,
                data={
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                    "client_id": self._client_id,
                    "device_code": code.device_code,
                },
            )
            payload = response.json()

            if response.status_code == 200:
                token = Token(
                    access_token=str(payload["access_token"]),
                    refresh_token=str(payload.get("refresh_token", "")),
                    expires_at=_expires_at(payload),
                    account=account,
                )
                self._save(token)
                return token

            error = str(payload.get("error", ""))
            if error == "authorization_pending":
                continue
            if error == "slow_down":
                interval += 5
                continue
            if error == "authorization_declined":
                raise AuthError("sign-in was declined in the browser")
            if error == "expired_token":
                break
            raise AuthError(f"sign-in failed: {payload.get('error_description', error)}")

        raise AuthError("the device code expired before sign-in completed; start again")

    # ------------------------------------------------------------------
    # Use
    # ------------------------------------------------------------------

    def access_token(self) -> str:
        """A valid access token, refreshing silently when the stored one is stale."""
        token = self.stored_token()
        if token is None:
            raise NotSignedInError("not signed in. Run 'jobbot mail login' first.")
        if token.is_fresh:
            return token.access_token
        return self._refresh(token).access_token

    def _refresh(self, token: Token) -> Token:
        self._require_client_id()
        if not token.refresh_token:
            raise NotSignedInError("stored token cannot be refreshed; sign in again")

        response = self._client.post(
            TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "client_id": self._client_id,
                "refresh_token": token.refresh_token,
                "scope": " ".join(SCOPES),
            },
        )
        if response.status_code != 200:
            raise NotSignedInError(
                "could not refresh the stored token; run 'jobbot mail login' again "
                f"({response.text[:200]})"
            )
        payload = response.json()
        refreshed = Token(
            access_token=str(payload["access_token"]),
            # Microsoft rotates refresh tokens; keep the old one if none returned.
            refresh_token=str(payload.get("refresh_token") or token.refresh_token),
            expires_at=_expires_at(payload),
            account=token.account,
        )
        self._save(refreshed)
        logger.info("refreshed the Mail.Send token")
        return refreshed
