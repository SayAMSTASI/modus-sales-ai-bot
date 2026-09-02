from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import McpServerConfig


class McpDiscoveryError(RuntimeError):
    """Raised when an MCP server cannot return its tool catalog."""


@dataclass(frozen=True)
class McpToolInfo:
    name: str
    description: str


class McpDiscoveryClient:
    def __init__(self, *, timeout_seconds: float = 20.0) -> None:
        self.timeout_seconds = timeout_seconds

    @staticmethod
    def _payloads(response: httpx.Response) -> list[dict[str, Any]]:
        if not response.content:
            return []
        content_type = response.headers.get("content-type", "").lower()
        if "text/event-stream" not in content_type:
            body = response.json()
            return [body] if isinstance(body, dict) else []

        payloads: list[dict[str, Any]] = []
        for line in response.text.splitlines():
            if not line.startswith("data:"):
                continue
            raw = line.removeprefix("data:").strip()
            if not raw:
                continue
            body = json.loads(raw)
            if isinstance(body, dict):
                payloads.append(body)
        return payloads

    @staticmethod
    def _result(response: httpx.Response, request_id: int) -> dict[str, Any]:
        response.raise_for_status()
        payloads = McpDiscoveryClient._payloads(response)
        body = next((item for item in payloads if item.get("id") == request_id), None)
        if body is None:
            raise McpDiscoveryError("MCP response has no matching JSON-RPC result")
        error = body.get("error")
        if isinstance(error, dict):
            message = str(error.get("message") or "unknown MCP error")
            raise McpDiscoveryError(message)
        result = body.get("result")
        if not isinstance(result, dict):
            raise McpDiscoveryError("MCP JSON-RPC result is invalid")
        return result

    def _initialize(
        self,
        client: httpx.Client,
        server: McpServerConfig,
    ) -> dict[str, str]:
        request_id = 1
        response = client.post(
            server.server_url,
            json={
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {
                        "name": "modus-sales-bot",
                        "version": "0.1.0",
                    },
                },
            },
        )
        result = self._result(response, request_id)
        protocol_version = str(result.get("protocolVersion") or "2025-06-18")
        headers = {"MCP-Protocol-Version": protocol_version}
        session_id = response.headers.get("mcp-session-id")
        if session_id:
            headers["Mcp-Session-Id"] = session_id
        initialized = client.post(
            server.server_url,
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
            },
        )
        initialized.raise_for_status()
        return headers

    def list_tools(self, server: McpServerConfig, access_token: str) -> list[McpToolInfo]:
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }
        with httpx.Client(
            timeout=self.timeout_seconds,
            follow_redirects=True,
            headers=headers,
        ) as client:
            session_headers = self._initialize(client, server)

            tools: list[McpToolInfo] = []
            cursor: str | None = None
            for request_id in range(2, 12):
                params: dict[str, str] = {}
                if cursor:
                    params["cursor"] = cursor
                response = client.post(
                    server.server_url,
                    headers=session_headers,
                    json={
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "method": "tools/list",
                        "params": params,
                    },
                )
                result = self._result(response, request_id)
                raw_tools = result.get("tools") or []
                if not isinstance(raw_tools, list):
                    raise McpDiscoveryError("MCP tools/list returned an invalid tool list")
                for item in raw_tools:
                    if not isinstance(item, dict) or not item.get("name"):
                        continue
                    tools.append(
                        McpToolInfo(
                            name=str(item["name"]),
                            description=str(item.get("description") or ""),
                        )
                    )
                next_cursor = result.get("nextCursor")
                if not isinstance(next_cursor, str) or not next_cursor:
                    break
                cursor = next_cursor
            else:
                raise McpDiscoveryError("MCP tools/list pagination limit exceeded")
            return tools

    def call_tool(
        self,
        server: McpServerConfig,
        access_token: str,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        if tool_name not in server.allowed_tools:
            raise McpDiscoveryError(
                f"Tool {tool_name!r} is not in the configured read-only allowlist"
            )
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }
        with httpx.Client(
            timeout=self.timeout_seconds,
            follow_redirects=True,
            headers=headers,
        ) as client:
            session_headers = self._initialize(client, server)
            request_id = 2
            response = client.post(
                server.server_url,
                headers=session_headers,
                json={
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": "tools/call",
                    "params": {
                        "name": tool_name,
                        "arguments": arguments,
                    },
                },
            )
            result = self._result(response, request_id)
            if result.get("isError"):
                raise McpDiscoveryError("MCP tool returned an error result")
            return result
