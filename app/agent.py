from __future__ import annotations

import json
import logging
import mimetypes
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import Any, Protocol
from uuid import uuid4

from openai import OpenAI

from app.attachments import AgentAttachment, AttachmentError, extract_response_attachments
from app.config import Settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AgentUsage:
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0


@dataclass(frozen=True)
class AgentResult:
    text: str
    request_id: str
    model: str
    duration_ms: int
    usage: AgentUsage
    attachments: tuple[AgentAttachment, ...] = ()


class McpUnavailableError(RuntimeError):
    """Raised when a requested MCP server cannot be attached safely."""


class AgentClient(Protocol):
    def respond(
        self,
        *,
        messages: list[dict[str, Any]],
        safety_identifier: str,
        allowed_tools: list[str],
        required_mcp_server: str | None = None,
        mcp_access_token: str | None = None,
        instruction_overrides: dict[str, str] | None = None,
    ) -> AgentResult: ...


def load_project_instructions(
    project_dir: Path,
    skill_overrides: dict[str, str] | None = None,
) -> str:
    skill_overrides = skill_overrides or {}
    parts: list[str] = []
    prompt = project_dir / "prompt.md"
    if prompt.exists():
        parts.append(prompt.read_text(encoding="utf-8"))
    skills_dir = project_dir / "skills"
    if skills_dir.exists():
        for path in sorted(skills_dir.glob("*/SKILL.md")):
            skill_content = skill_overrides.get(
                path.parent.name,
                path.read_text(encoding="utf-8"),
            )
            parts.append(f"\n# Skill: {path.parent.name}\n{skill_content}")
            references_dir = path.parent / "references"
            if references_dir.exists():
                for reference in sorted(references_dir.glob("*.md")):
                    parts.append(
                        f"\n## Skill reference: {path.parent.name}/{reference.name}\n"
                        f"{reference.read_text(encoding='utf-8')}"
                    )
    if not parts:
        raise RuntimeError(f"Project prompt bundle is empty: {project_dir}")
    return "\n\n".join(parts)


def calculate_cost_usd(usage: AgentUsage, settings: Settings) -> float:
    uncached = max(usage.input_tokens - usage.cached_input_tokens, 0)
    cost = (
        uncached * settings.openai_input_usd_per_mtok
        + usage.cached_input_tokens * settings.openai_cached_input_usd_per_mtok
        + usage.output_tokens * settings.openai_output_usd_per_mtok
    ) / 1_000_000
    return round(cost, 8)


class MockAgentClient:
    def __init__(self, model: str = "mock-sales-agent") -> None:
        self.model = model
        self.calls = 0
        self.last_allowed_tools: list[str] = []
        self.last_required_mcp_server: str | None = None
        self.last_messages: list[dict[str, Any]] = []
        self.last_mcp_access_token: str | None = None
        self.last_instruction_overrides: dict[str, str] = {}
        self.next_attachments: tuple[AgentAttachment, ...] = ()

    def respond(
        self,
        *,
        messages: list[dict[str, Any]],
        safety_identifier: str,
        allowed_tools: list[str],
        required_mcp_server: str | None = None,
        mcp_access_token: str | None = None,
        instruction_overrides: dict[str, str] | None = None,
    ) -> AgentResult:
        self.calls += 1
        self.last_allowed_tools = allowed_tools
        self.last_required_mcp_server = required_mcp_server
        self.last_messages = messages
        self.last_mcp_access_token = mcp_access_token
        self.last_instruction_overrides = instruction_overrides or {}
        latest_content = next(
            (item["content"] for item in reversed(messages) if item["role"] == "user"),
            "",
        )
        if isinstance(latest_content, list):
            latest = " ".join(
                str(item.get("text") or item.get("filename") or "")
                for item in latest_content
                if isinstance(item, dict)
            ).strip()
        else:
            latest = str(latest_content)
        answer = f"Демо-ответ sales-агента: {latest[:500]}"
        return AgentResult(
            text=answer,
            request_id=f"mock-{uuid4().hex}",
            model=self.model,
            duration_ms=5,
            usage=AgentUsage(
                input_tokens=max(len(latest) // 3, 1),
                output_tokens=max(len(answer) // 3, 1),
            ),
            attachments=self.next_attachments,
        )


class OpenAIAgentClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.client = OpenAI(api_key=settings.openai_api_key)

    def _tools(
        self,
        allowed_tools: list[str],
        required_mcp_server: str | None = None,
        mcp_access_token: str | None = None,
    ) -> list[dict[str, Any]]:
        allowed = set(allowed_tools)
        tools: list[dict[str, Any]] = []
        for server in self.settings.mcp_servers():
            if required_mcp_server and server.server_label != required_mcp_server:
                continue
            selected = [
                name
                for name in server.allowed_tools
                if name in allowed or f"{server.server_label}:{name}" in allowed
            ]
            if not selected:
                continue
            item: dict[str, Any] = {
                "type": "mcp",
                "server_label": server.server_label,
                "server_description": server.server_description,
                "server_url": server.server_url,
                "allowed_tools": selected,
                "require_approval": "never",
            }
            if server.authorization_type == "oauth" or server.authorization_env:
                token = mcp_access_token or ""
                if not token:
                    if required_mcp_server:
                        raise McpUnavailableError(
                            f"MCP server {server.server_label} has no access token"
                        )
                    continue
                item["authorization"] = token
            tools.append(item)
        if required_mcp_server and not tools:
            raise McpUnavailableError(
                f"MCP server {required_mcp_server} has no allowed tools"
            )
        if self.settings.openai_enable_image_generation:
            tools.append({"type": "image_generation"})
        if self.settings.openai_enable_code_interpreter:
            tools.append(
                {
                    "type": "code_interpreter",
                    "container": {
                        "type": "auto",
                        "memory_limit": self.settings.openai_code_interpreter_memory_limit,
                    },
                }
            )
        return tools

    def _download_container_attachment(self, attachment: AgentAttachment) -> AgentAttachment:
        if not attachment.source_container_id or not attachment.source_file_id:
            return attachment
        content = self.client.containers.files.content.retrieve(
            attachment.source_file_id,
            container_id=attachment.source_container_id,
        )
        chunks: list[bytes] = []
        total = 0
        try:
            for chunk in content.iter_bytes():
                total += len(chunk)
                if total > self.settings.attachment_max_output_bytes:
                    raise AttachmentError("Generated file exceeds the configured output limit")
                chunks.append(chunk)
        finally:
            content.close()
        mime_type = (
            mimetypes.guess_type(attachment.filename)[0]
            or attachment.mime_type
            or "application/octet-stream"
        ).lower()
        if mime_type not in self.settings.allowed_attachment_mime_types():
            raise AttachmentError("Generated file MIME type is not allowed")
        return AgentAttachment(
            filename=attachment.filename,
            mime_type=mime_type,
            kind="photo" if mime_type in {"image/jpeg", "image/png", "image/webp"} else "document",
            data=b"".join(chunks),
            source_server="openai",
        )

    def respond(
        self,
        *,
        messages: list[dict[str, Any]],
        safety_identifier: str,
        allowed_tools: list[str],
        required_mcp_server: str | None = None,
        mcp_access_token: str | None = None,
        instruction_overrides: dict[str, str] | None = None,
    ) -> AgentResult:
        started = monotonic()
        kwargs: dict[str, Any] = {
            "model": self.settings.openai_model,
            "input": messages,
            "tools": self._tools(
                allowed_tools,
                required_mcp_server,
                mcp_access_token,
            ),
            "store": False,
            "safety_identifier": safety_identifier,
            "max_output_tokens": self.settings.openai_max_output_tokens,
            "reasoning": {
                "effort": self.settings.openai_reasoning_effort,
                "context": "current_turn",
            },
        }
        if required_mcp_server:
            kwargs["tool_choice"] = "required"
        if self.settings.openai_prompt_id:
            prompt: dict[str, str] = {"id": self.settings.openai_prompt_id}
            if self.settings.openai_prompt_version:
                prompt["version"] = self.settings.openai_prompt_version
            kwargs["prompt"] = prompt
        else:
            kwargs["instructions"] = load_project_instructions(
                self.settings.project_dir,
                instruction_overrides,
            )
        try:
            response = self.client.responses.create(**kwargs)
        except Exception:
            if required_mcp_server or not any(
                tool.get("type") == "mcp" for tool in kwargs["tools"]
            ):
                raise
            logger.warning("OpenAI request with optional MCP failed; retrying without MCP")
            kwargs["tools"] = [
                tool for tool in kwargs["tools"] if tool.get("type") != "mcp"
            ]
            response = self.client.responses.create(**kwargs)
        response_usage = getattr(response, "usage", None)
        details = getattr(response_usage, "input_tokens_details", None)
        usage = AgentUsage(
            input_tokens=getattr(response_usage, "input_tokens", 0) or 0,
            cached_input_tokens=getattr(details, "cached_tokens", 0) or 0,
            output_tokens=getattr(response_usage, "output_tokens", 0) or 0,
        )
        attachments = extract_response_attachments(
            response,
            max_count=self.settings.attachment_max_output_count,
            max_bytes=self.settings.attachment_max_output_bytes,
        )
        materialized_attachments: list[AgentAttachment] = []
        for attachment in attachments:
            try:
                materialized_attachments.append(self._download_container_attachment(attachment))
            except Exception:
                logger.warning(
                    "Generated OpenAI attachment skipped name=%s",
                    attachment.filename,
                    exc_info=True,
                )
        return AgentResult(
            text=response.output_text or "",
            request_id=response.id,
            model=response.model,
            duration_ms=int((monotonic() - started) * 1000),
            usage=usage,
            attachments=tuple(materialized_attachments),
        )


def build_agent_client(settings: Settings) -> AgentClient:
    if settings.agent_backend == "openai":
        return OpenAIAgentClient(settings)
    return MockAgentClient()


def parse_allowed_tools(value: str) -> list[str]:
    parsed = json.loads(value or "[]")
    return [str(item) for item in parsed] if isinstance(parsed, list) else []
