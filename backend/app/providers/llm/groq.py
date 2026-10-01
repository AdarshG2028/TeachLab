"""Groq LLM provider via its OpenAI-compatible endpoint."""

from __future__ import annotations

import openai
from openai import AsyncOpenAI

from app.core.config import Settings
from app.core.errors import ConfigurationError, ProviderError
from app.core.logging import get_logger
from app.core.retry import TokenBucket, with_retries
from app.core.tokens import count_tokens
from app.providers.llm.base import ChatMessage, LLMProvider, LLMResult, LLMUsage, Tier

log = get_logger(__name__)


class GroqLLMProvider(LLMProvider):
    name = "groq"

    def __init__(self, settings: Settings, client: AsyncOpenAI | None = None) -> None:
        if client is None and not settings.groq_api_key:
            raise ConfigurationError("GROQ_API_KEY is not set (or set LLM_PROVIDER=fake for tests)")
        self.settings = settings
        self.client = client or AsyncOpenAI(
            api_key=settings.groq_api_key,
            base_url=settings.groq_base_url,
            timeout=settings.llm_timeout_seconds,
            max_retries=0,  # retries handled here so rate-limit hints are honored uniformly
        )
        self.bucket = TokenBucket(settings.llm_tokens_per_minute)

    def model_for(self, tier: Tier) -> str:
        return self.settings.llm_model_fast if tier == "fast" else self.settings.llm_model_smart

    def context_tokens(self, tier: Tier) -> int:
        return self.settings.llm_context_tokens_fast if tier == "fast" else self.settings.llm_context_tokens_smart

    async def complete(
        self,
        messages: list[ChatMessage],
        *,
        task: str,
        tier: Tier = "smart",
        json_mode: bool = False,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> LLMResult:
        model = self.model_for(tier)
        max_out = max_tokens or self.settings.llm_max_output_tokens
        estimate = sum(count_tokens(m.content) for m in messages) + max_out // 2
        await self.bucket.acquire(estimate)

        kwargs: dict = {
            "model": model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "max_tokens": max_out,
            "temperature": self.settings.llm_temperature if temperature is None else temperature,
        }
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}

        async def _call() -> LLMResult:
            try:
                resp = await self.client.chat.completions.create(**kwargs)
            except openai.RateLimitError as exc:
                raise ProviderError("LLM rate limited", retryable=True,
                                    retry_after=_retry_after(exc.response)) from exc
            except (openai.APITimeoutError, openai.APIConnectionError) as exc:
                raise ProviderError(f"LLM connection problem: {exc}", retryable=True) from exc
            except openai.InternalServerError as exc:
                raise ProviderError(f"LLM server error: {exc}", retryable=True) from exc
            except openai.BadRequestError as exc:
                # Groq returns 400 json_validate_failed when the model emits invalid JSON in json mode;
                # one more sample usually succeeds.
                retry = "json_validate_failed" in str(exc)
                raise ProviderError(f"LLM rejected request: {exc}", retryable=retry) from exc
            except openai.APIStatusError as exc:
                raise ProviderError(f"LLM error {exc.status_code}: {exc}", retryable=exc.status_code >= 500) from exc
            choice = resp.choices[0]
            usage = LLMUsage(
                prompt_tokens=getattr(resp.usage, "prompt_tokens", 0) or 0,
                completion_tokens=getattr(resp.usage, "completion_tokens", 0) or 0,
            )
            return LLMResult(text=choice.message.content or "", model=resp.model or model, usage=usage)

        result = await with_retries(_call, attempts=self.settings.llm_max_retries, op=f"llm:{task}")
        log.info("llm_call", task=task, model=result.model, prompt_tokens=result.usage.prompt_tokens,
                 completion_tokens=result.usage.completion_tokens)
        return result


def _retry_after(response) -> float | None:
    try:
        value = response.headers.get("retry-after")
        return float(value) if value else None
    except Exception:
        return None
