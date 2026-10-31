"""Pluggable LLM client (anthropic | openai | gemini) behind one interface."""

from __future__ import annotations

import json
import logging
import time
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from config import get_settings
from exceptions import LLMError, ValidationFallbackError

logger = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)


class LLMClient:
    """
    Provider-agnostic LLM facade.

    Supports structured JSON output validated by Pydantic, optional image input,
    and a simple tool-calling loop. Model names come from settings / env.
    """

    def __init__(self) -> None:
        self.settings = get_settings()
        self.provider = self.settings.llm_provider.lower().strip()
        self.timeout = self.settings.llm_timeout_seconds

    @property
    def model_name(self) -> str:
        if self.provider == "openai":
            return self.settings.openai_model
        if self.provider == "gemini":
            return self.settings.gemini_model
        return self.settings.anthropic_model

    def has_credentials(self) -> bool:
        """Return True if the configured provider has an API key."""
        if self.provider == "openai":
            return bool(self.settings.openai_api_key)
        if self.provider == "gemini":
            return bool(self.settings.gemini_api_key)
        return bool(self.settings.anthropic_api_key)

    def complete_json(
        self,
        *,
        system: str,
        user: str,
        schema_model: type[T],
        image_bytes: bytes | None = None,
        image_mime: str = "image/jpeg",
        max_retries: int = 2,
        fallback: T | None = None,
    ) -> T:
        """
        Ask the LLM for JSON matching schema_model; validate with retries.

        On exhaustion, return fallback or raise ValidationFallbackError.
        """
        last_error: Exception | None = None
        schema_hint = json.dumps(schema_model.model_json_schema(), indent=2)
        prompt = (
            f"{user}\n\nRespond with ONLY valid JSON matching this schema:\n{schema_hint}"
        )
        for attempt in range(max_retries + 1):
            try:
                raw = self._complete_raw(system=system, user=prompt, image_bytes=image_bytes, image_mime=image_mime)
                data = self._extract_json(raw)
                return schema_model.model_validate(data)
            except (LLMError, ValidationError, json.JSONDecodeError, ValueError) as exc:
                last_error = exc
                logger.warning("LLM JSON attempt %s failed: %s", attempt + 1, exc)
                prompt = (
                    f"{user}\n\nPrevious output was invalid ({exc}). "
                    f"Return ONLY valid JSON for schema:\n{schema_hint}"
                )
        if fallback is not None:
            logger.error("Using deterministic fallback after LLM validation failures")
            return fallback
        raise ValidationFallbackError(str(last_error))

    def complete_text(
        self,
        *,
        system: str,
        user: str,
        image_bytes: bytes | None = None,
        image_mime: str = "image/jpeg",
    ) -> str:
        """Free-text completion with timeout."""
        return self._complete_raw(
            system=system, user=user, image_bytes=image_bytes, image_mime=image_mime
        )

    def tool_call_loop(
        self,
        *,
        system: str,
        user: str,
        tools: list[dict[str, Any]],
        tool_handler: Any,
        max_rounds: int = 4,
    ) -> tuple[str, list[str]]:
        """
        Minimal tool-calling loop.

        For demo reliability this uses a structured prompt asking the model to
        emit either a tool request JSON or a final JSON answer. Providers with
        native tools are preferred when credentials exist; otherwise a single
        text turn is used and the caller applies deterministic logic.
        """
        tools_called: list[str] = []
        if not self.has_credentials():
            raise LLMError("No LLM credentials configured")

        transcript = user
        for _ in range(max_rounds):
            tool_spec = json.dumps(tools, indent=2)
            prompt = (
                f"{transcript}\n\nAvailable tools:\n{tool_spec}\n\n"
                'Either call a tool as {"tool":"name","arguments":{...}} '
                'or finish with {"final":{...}}.'
            )
            raw = self._complete_raw(system=system, user=prompt)
            data = self._extract_json(raw)
            if "final" in data:
                return json.dumps(data["final"]), tools_called
            if "tool" in data:
                name = data["tool"]
                args = data.get("arguments", {})
                tools_called.append(name)
                result = tool_handler(name, args)
                transcript += f"\nTool {name} result: {json.dumps(result, default=str)}"
                continue
            return raw, tools_called
        raise LLMError("Tool loop exceeded max rounds")

    def _complete_raw(
        self,
        *,
        system: str,
        user: str,
        image_bytes: bytes | None = None,
        image_mime: str = "image/jpeg",
    ) -> str:
        if not self.has_credentials():
            raise LLMError("No LLM credentials configured")
        start = time.time()
        try:
            if self.provider == "openai":
                text = self._openai(system, user, image_bytes, image_mime)
            elif self.provider == "gemini":
                text = self._gemini(system, user, image_bytes, image_mime)
            else:
                text = self._anthropic(system, user, image_bytes, image_mime)
        except httpx.TimeoutException as exc:
            raise LLMError(f"LLM timeout after {self.timeout}s") from exc
        except httpx.HTTPError as exc:
            raise LLMError(f"LLM HTTP error: {exc}") from exc
        elapsed = time.time() - start
        if elapsed > self.timeout:
            raise LLMError(f"LLM exceeded soft timeout ({elapsed:.1f}s > {self.timeout}s)")
        return text

    def _anthropic(
        self, system: str, user: str, image_bytes: bytes | None, image_mime: str
    ) -> str:
        import base64

        content: list[dict[str, Any]] = []
        if image_bytes:
            content.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": image_mime,
                        "data": base64.b64encode(image_bytes).decode("ascii"),
                    },
                }
            )
        content.append({"type": "text", "text": user})
        payload = {
            "model": self.model_name,
            "max_tokens": 1024,
            "system": system,
            "messages": [{"role": "user", "content": content}],
        }
        with httpx.Client(timeout=self.timeout) as client:
            resp = client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": self.settings.anthropic_api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
        parts = data.get("content", [])
        return "".join(p.get("text", "") for p in parts if p.get("type") == "text")

    def _openai(
        self, system: str, user: str, image_bytes: bytes | None, image_mime: str
    ) -> str:
        import base64

        user_content: Any
        if image_bytes:
            b64 = base64.b64encode(image_bytes).decode("ascii")
            user_content = [
                {"type": "text", "text": user},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{image_mime};base64,{b64}"},
                },
            ]
        else:
            user_content = user
        payload = {
            "model": self.model_name,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user_content},
            ],
            "response_format": {"type": "json_object"},
        }
        with httpx.Client(timeout=self.timeout) as client:
            resp = client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.settings.openai_api_key}",
                    "content-type": "application/json",
                },
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
        return data["choices"][0]["message"]["content"]

    def _gemini(
        self, system: str, user: str, image_bytes: bytes | None, image_mime: str
    ) -> str:
        import base64

        parts: list[dict[str, Any]] = [{"text": f"{system}\n\n{user}"}]
        if image_bytes:
            parts.append(
                {
                    "inline_data": {
                        "mime_type": image_mime,
                        "data": base64.b64encode(image_bytes).decode("ascii"),
                    }
                }
            )
        payload = {"contents": [{"parts": parts}]}
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{self.model_name}:generateContent"
        )
        with httpx.Client(timeout=self.timeout) as client:
            resp = client.post(
                url,
                params={"key": self.settings.gemini_api_key},
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
        return data["candidates"][0]["content"]["parts"][0]["text"]

    @staticmethod
    def _extract_json(raw: str) -> dict[str, Any]:
        text = raw.strip()
        if text.startswith("```"):
            lines = text.splitlines()
            lines = [ln for ln in lines if not ln.startswith("```")]
            text = "\n".join(lines).strip()
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end < 0:
            raise ValueError("No JSON object found in LLM output")
        return json.loads(text[start : end + 1])
