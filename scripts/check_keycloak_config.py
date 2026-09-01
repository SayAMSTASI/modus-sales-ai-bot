from __future__ import annotations

import sys

import httpx

from app.config import Settings


def fail(message: str) -> None:
    raise SystemExit(f"keycloak-config-error: {message}")


def main() -> None:
    settings = Settings()
    if not settings.keycloak_client_id:
        fail("KEYCLOAK_CLIENT_ID is empty")
    if settings.keycloak_flow == "authorization_code" and not settings.public_base_url.startswith(
        "https://"
    ):
        fail("authorization_code requires HTTPS PUBLIC_BASE_URL")

    protected_resource_url = (
        f"{settings.keycloak_resource.rstrip('/')}/.well-known/oauth-protected-resource"
    )
    oidc_url = f"{settings.keycloak_issuer.rstrip('/')}/.well-known/openid-configuration"
    with httpx.Client(timeout=settings.oauth_http_timeout_seconds) as client:
        protected = client.get(protected_resource_url)
        protected.raise_for_status()
        oidc = client.get(oidc_url)
        oidc.raise_for_status()
    protected_body = protected.json()
    oidc_body = oidc.json()

    if protected_body.get("resource") != settings.keycloak_resource:
        fail("protected resource metadata does not match KEYCLOAK_RESOURCE")
    authorization_servers = protected_body.get("authorization_servers") or []
    if settings.keycloak_issuer not in authorization_servers:
        fail("KEYCLOAK_ISSUER is not advertised by the MCP protected resource")
    if "header" not in (protected_body.get("bearer_methods_supported") or []):
        fail("MCP does not advertise Bearer authorization header support")
    supported_scopes = set(protected_body.get("scopes_supported") or [])
    required_resource_scopes = {
        scope for scope in settings.keycloak_scopes.split() if scope != "offline_access"
    }
    if not required_resource_scopes.issubset(supported_scopes):
        fail("MCP metadata does not advertise all configured resource scopes")

    grants = set(oidc_body.get("grant_types_supported") or [])
    required_grant = (
        "authorization_code"
        if settings.keycloak_flow == "authorization_code"
        else "urn:ietf:params:oauth:grant-type:device_code"
    )
    if required_grant not in grants or "refresh_token" not in grants:
        fail("issuer does not advertise the configured login flow and refresh_token")
    if settings.keycloak_flow == "authorization_code" and "S256" not in (
        oidc_body.get("code_challenge_methods_supported") or []
    ):
        fail("issuer does not advertise PKCE S256")

    print(
        "keycloak-config-ok "
        f"flow={settings.keycloak_flow} "
        f"issuer={settings.keycloak_issuer} "
        f"resource={settings.keycloak_resource} "
        f"redirect_uri={settings.oauth_redirect_uri}"
    )


if __name__ == "__main__":
    try:
        main()
    except httpx.HTTPError as exc:
        print(f"keycloak-config-error: metadata request failed: {type(exc).__name__}")
        sys.exit(1)
