from __future__ import annotations

import argparse
import json
from pathlib import Path

import httpx
from sqlalchemy import select

from app.config import Settings
from app.db import make_engine, make_session_factory
from app.mcp_discovery import McpDiscoveryClient, McpDiscoveryError
from app.models import OAuthCredential
from app.oauth import OAuthConfigurationError, OAuthLoginRequired, OAuthTokenStore


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="List MCP tool metadata without invoking any MCP tool."
    )
    parser.add_argument("server", help="MCP server label from config/mcp_servers.json")
    parser.add_argument("--telegram-user-id", type=int)
    parser.add_argument(
        "--key-file",
        type=Path,
        default=Path("data/token-encryption.key"),
        help="Local Fernet key file; ignored when TOKEN_ENCRYPTION_KEY is set.",
    )
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument("--call", metavar="TOOL", help="Call one allowlisted tool")
    parser.add_argument(
        "--arguments",
        default="{}",
        help="JSON object passed to --call (default: {}).",
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="Do not print tool result content; print only success metadata.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    base = Settings()
    encryption_key = base.token_encryption_key
    if not encryption_key and args.key_file.is_file():
        encryption_key = args.key_file.read_text(encoding="ascii").strip()
    keycloak_client_id = base.keycloak_client_id
    local_client_id_file = Path("data/keycloak-client-id.txt")
    if not keycloak_client_id and local_client_id_file.is_file():
        keycloak_client_id = local_client_id_file.read_text(encoding="ascii").strip()
    settings = Settings(
        token_encryption_key=encryption_key,
        keycloak_client_id=keycloak_client_id,
    )
    server = next(
        (item for item in settings.mcp_servers() if item.server_label == args.server),
        None,
    )
    if server is None:
        labels = ", ".join(item.server_label for item in settings.mcp_servers())
        raise SystemExit(f"Unknown MCP server {args.server!r}. Available: {labels}")

    engine = make_engine(settings)
    factory = make_session_factory(engine)
    oauth_store = OAuthTokenStore(settings)
    with factory() as session:
        user_ids = list(session.scalars(select(OAuthCredential.telegram_user_id)))
        if args.telegram_user_id is not None:
            user_id = args.telegram_user_id
            if user_id not in user_ids:
                raise SystemExit("No saved OAuth credential for this Telegram user.")
        elif len(user_ids) == 1:
            user_id = user_ids[0]
        else:
            raise SystemExit(
                "Pass --telegram-user-id because the database does not contain exactly "
                "one OAuth credential."
            )
        token = oauth_store.access_token(session, user_id)
        session.commit()
    if not token:
        raise SystemExit("OAuth authorization is required. Run /login in Telegram.")

    client = McpDiscoveryClient(timeout_seconds=settings.oauth_http_timeout_seconds)
    if args.call:
        try:
            arguments = json.loads(args.arguments)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"Invalid --arguments JSON: {exc}") from exc
        if not isinstance(arguments, dict):
            raise SystemExit("--arguments must be a JSON object.")
        result = client.call_tool(server, token, args.call, arguments)
        if args.check_only:
            content = result.get("content") or []
            count = len(content) if isinstance(content, list) else 0
            print(
                f"mcp-tool-check-ok server={server.server_label} "
                f"tool={args.call} content_items={count}"
            )
        else:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    tools = client.list_tools(server, token)
    if args.as_json:
        print(
            json.dumps(
                [
                    {"name": item.name, "description": item.description}
                    for item in tools
                ],
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print(f"{server.server_label}: {len(tools)} tools")
        for item in tools:
            print(f"- {item.name}: {item.description}")


if __name__ == "__main__":
    try:
        main()
    except (
        httpx.HTTPError,
        McpDiscoveryError,
        OAuthConfigurationError,
        OAuthLoginRequired,
    ) as exc:
        raise SystemExit(f"MCP operation failed: {exc}") from exc
