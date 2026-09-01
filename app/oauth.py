from __future__ import annotations

import base64
import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import OAuthCredential


class OAuthConfigurationError(RuntimeError):
    """Raised when an OAuth flow cannot start because settings are incomplete."""


class OAuthLoginRequired(RuntimeError):
    """Raised when a valid user token is not available."""


class OAuthProtocolError(RuntimeError):
    """Raised for an unexpected OAuth server response."""


@dataclass(frozen=True)
class DeviceAuthorization:
    device_code: str
    user_code: str
    verification_uri: str
    verification_uri_complete: str | None
    expires_in: int
    interval: int


@dataclass(frozen=True)
class AuthorizationCodeStart:
    authorization_url: str
    state: str
    code_verifier: str
    expires_in: int


@dataclass(frozen=True)
class TokenResponse:
    access_token: str
    refresh_token: str | None
    token_type: str
    scope: str
    expires_in: int
    refresh_expires_in: int | None


@dataclass(frozen=True)
class TokenPollResult:
    state: str
    token: TokenResponse | None = None
    next_interval: int | None = None


def oauth_state_hash(state: str) -> str:
    return hashlib.sha256(state.encode("utf-8")).hexdigest()


class TokenCipher:
    def __init__(self, key: str) -> None:
        if not key:
            raise OAuthConfigurationError("TOKEN_ENCRYPTION_KEY is not configured")
        try:
            self._fernet = Fernet(key.encode("ascii"))
        except (ValueError, UnicodeEncodeError) as exc:
            raise OAuthConfigurationError(
                "TOKEN_ENCRYPTION_KEY must be a Fernet urlsafe base64 key"
            ) from exc

    def encrypt(self, value: str) -> str:
        return self._fernet.encrypt(value.encode("utf-8")).decode("ascii")

    def decrypt(self, value: str) -> str:
        try:
            return self._fernet.decrypt(value.encode("ascii")).decode("utf-8")
        except (InvalidToken, UnicodeError) as exc:
            raise OAuthLoginRequired("Saved OAuth token cannot be decrypted") from exc


class KeycloakOAuthClient:
    def __init__(self, settings: Settings, http: httpx.Client | None = None) -> None:
        self.settings = settings
        self.http = http

    def _post(self, url: str, *, data: dict[str, str]) -> httpx.Response:
        if self.http is not None:
            return self.http.post(url, data=data)
        return httpx.post(
            url,
            data=data,
            timeout=self.settings.oauth_http_timeout_seconds,
        )

    def _ensure_configured(self) -> None:
        if not self.settings.keycloak_client_id:
            raise OAuthConfigurationError("KEYCLOAK_CLIENT_ID is not configured")

    def _resource(self, data: dict[str, str]) -> dict[str, str]:
        if self.settings.keycloak_resource:
            data["resource"] = self.settings.keycloak_resource
        return data

    @staticmethod
    def _token(body: dict[str, Any]) -> TokenResponse:
        access_token = body.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise OAuthProtocolError("OAuth token response has no access_token")
        refresh_expires = body.get("refresh_expires_in")
        return TokenResponse(
            access_token=access_token,
            refresh_token=body.get("refresh_token") or None,
            token_type=str(body.get("token_type") or "Bearer"),
            scope=str(body.get("scope") or ""),
            expires_in=max(int(body.get("expires_in") or 60), 1),
            refresh_expires_in=int(refresh_expires) if refresh_expires is not None else None,
        )

    def start_device_authorization(self) -> DeviceAuthorization:
        self._ensure_configured()
        response = self._post(
            self.settings.oauth_device_authorization_endpoint,
            data=self._resource(
                {
                    "client_id": self.settings.keycloak_client_id,
                    "scope": self.settings.keycloak_scopes,
                }
            ),
        )
        response.raise_for_status()
        body = response.json()
        required = ("device_code", "user_code", "verification_uri", "expires_in")
        if any(not body.get(name) for name in required):
            raise OAuthProtocolError("Device authorization response is incomplete")
        return DeviceAuthorization(
            device_code=str(body["device_code"]),
            user_code=str(body["user_code"]),
            verification_uri=str(body["verification_uri"]),
            verification_uri_complete=body.get("verification_uri_complete") or None,
            expires_in=int(body["expires_in"]),
            interval=max(int(body.get("interval") or 5), 1),
        )

    def start_authorization_code(self) -> AuthorizationCodeStart:
        self._ensure_configured()
        state = secrets.token_urlsafe(32)
        code_verifier = secrets.token_urlsafe(64)
        digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
        code_challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
        parameters = self._resource(
            {
                "response_type": "code",
                "client_id": self.settings.keycloak_client_id,
                "redirect_uri": self.settings.oauth_redirect_uri,
                "scope": self.settings.keycloak_scopes,
                "state": state,
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
            }
        )
        return AuthorizationCodeStart(
            authorization_url=f"{self.settings.oauth_authorization_endpoint}?{urlencode(parameters)}",
            state=state,
            code_verifier=code_verifier,
            expires_in=self.settings.oauth_authorization_ttl_seconds,
        )

    def exchange_authorization_code(self, code: str, code_verifier: str) -> TokenResponse:
        self._ensure_configured()
        response = self._post(
            self.settings.oauth_token_endpoint,
            data=self._resource(
                {
                    "grant_type": "authorization_code",
                    "client_id": self.settings.keycloak_client_id,
                    "code": code,
                    "redirect_uri": self.settings.oauth_redirect_uri,
                    "code_verifier": code_verifier,
                }
            ),
        )
        if response.status_code != 200:
            raise OAuthLoginRequired("OAuth authorization code is invalid or expired")
        return self._token(response.json())

    def poll_device_token(self, device_code: str, interval: int) -> TokenPollResult:
        self._ensure_configured()
        response = self._post(
            self.settings.oauth_token_endpoint,
            data=self._resource(
                {
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                    "client_id": self.settings.keycloak_client_id,
                    "device_code": device_code,
                }
            ),
        )
        if response.status_code == 200:
            return TokenPollResult(state="authorized", token=self._token(response.json()))
        body = response.json() if response.headers.get("content-type", "").startswith(
            "application/json"
        ) else {}
        error = str(body.get("error") or "oauth_error")
        if error == "authorization_pending":
            return TokenPollResult(state="pending", next_interval=interval)
        if error == "slow_down":
            return TokenPollResult(state="pending", next_interval=interval + 5)
        if error in {"expired_token", "access_denied"}:
            return TokenPollResult(state=error)
        raise OAuthProtocolError(f"OAuth device token error: {error}")

    def refresh(self, refresh_token: str) -> TokenResponse:
        self._ensure_configured()
        response = self._post(
            self.settings.oauth_token_endpoint,
            data=self._resource(
                {
                    "grant_type": "refresh_token",
                    "client_id": self.settings.keycloak_client_id,
                    "refresh_token": refresh_token,
                }
            ),
        )
        if response.status_code != 200:
            raise OAuthLoginRequired("OAuth session expired or was revoked")
        return self._token(response.json())

    def revoke(self, token: str) -> None:
        self._ensure_configured()
        response = self._post(
            self.settings.oauth_revocation_endpoint,
            data={"client_id": self.settings.keycloak_client_id, "token": token},
        )
        if response.status_code not in {200, 204, 400, 404, 405}:
            response.raise_for_status()


class OAuthTokenStore:
    def __init__(
        self,
        settings: Settings,
        client: KeycloakOAuthClient | None = None,
    ) -> None:
        self.settings = settings
        self.client = client or KeycloakOAuthClient(settings)

    def cipher(self) -> TokenCipher:
        return TokenCipher(self.settings.token_encryption_key)

    def save(self, session: Session, telegram_user_id: int, token: TokenResponse) -> None:
        now = datetime.now(UTC)
        cipher = self.cipher()
        credential = session.scalar(
            select(OAuthCredential).where(
                OAuthCredential.telegram_user_id == telegram_user_id
            )
        )
        if credential is None:
            credential = OAuthCredential(
                telegram_user_id=telegram_user_id,
                access_token_encrypted="",
                expires_at=now,
            )
            session.add(credential)
        credential.access_token_encrypted = cipher.encrypt(token.access_token)
        if token.refresh_token:
            credential.refresh_token_encrypted = cipher.encrypt(token.refresh_token)
        credential.token_type = token.token_type
        credential.scope = token.scope
        credential.expires_at = now + timedelta(seconds=token.expires_in)
        credential.refresh_expires_at = (
            now + timedelta(seconds=token.refresh_expires_in)
            if token.refresh_expires_in is not None
            else None
        )
        session.flush()

    def access_token(self, session: Session, telegram_user_id: int) -> str | None:
        credential = session.scalar(
            select(OAuthCredential).where(
                OAuthCredential.telegram_user_id == telegram_user_id
            )
        )
        if credential is None:
            return None
        cipher = self.cipher()
        now = datetime.now(UTC)
        expires_at = credential.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        if expires_at > now + timedelta(seconds=30):
            return cipher.decrypt(credential.access_token_encrypted)
        if not credential.refresh_token_encrypted:
            session.delete(credential)
            session.flush()
            raise OAuthLoginRequired("OAuth access token expired")
        try:
            refreshed = self.client.refresh(cipher.decrypt(credential.refresh_token_encrypted))
        except OAuthLoginRequired:
            session.delete(credential)
            session.flush()
            raise
        self.save(session, telegram_user_id, refreshed)
        return refreshed.access_token

    def logout(self, session: Session, telegram_user_id: int) -> bool:
        credential = session.scalar(
            select(OAuthCredential).where(
                OAuthCredential.telegram_user_id == telegram_user_id
            )
        )
        if credential is None:
            return False
        cipher = self.cipher()
        encrypted = credential.refresh_token_encrypted or credential.access_token_encrypted
        try:
            self.client.revoke(cipher.decrypt(encrypted))
        except (OAuthConfigurationError, OAuthLoginRequired, httpx.HTTPError):
            pass
        session.delete(credential)
        session.flush()
        return True

    def delete(self, session: Session, telegram_user_id: int) -> None:
        session.execute(
            delete(OAuthCredential).where(
                OAuthCredential.telegram_user_id == telegram_user_id
            )
        )
