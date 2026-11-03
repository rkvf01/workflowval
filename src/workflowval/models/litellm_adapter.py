"""The default adapter. This is the only module that imports a provider library (NFR-5)."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from workflowval.models.base import Message, ModelError, Reply, ToolCall, ToolSpec, Usage
from workflowval.redact import redact_text


def _load_litellm() -> Any:
    # Without this, importing litellm fetches its price list from the network.
    os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    import litellm

    return litellm


# Seconds to wait before each new try. A temporary provider error shouldn't end a whole run.
RETRY_DELAYS = (2.0, 4.0, 8.0, 16.0, 32.0)
_TEMPORARY_ERRORS = frozenset(
    {
        "APIConnectionError",
        "APITimeoutError",
        "InternalServerError",
        "RateLimitError",
        "ServiceUnavailableError",
        "Timeout",
    }
)


def _is_temporary(exc: Exception) -> bool:
    """True for rate limits, provider outages and dropped connections, which may clear."""
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status == 429 or status >= 500
    return type(exc).__name__ in _TEMPORARY_ERRORS


def _to_wire(message: Message) -> dict[str, Any]:
    wire: dict[str, Any] = {"role": message.role, "content": message.content}
    if message.tool_calls:
        wire["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.name, "arguments": json.dumps(call.arguments)},
            }
            for call in message.tool_calls
        ]
    if message.tool_call_id is not None:
        wire["tool_call_id"] = message.tool_call_id
    return wire


def _from_wire(raw: Any) -> ToolCall:
    name = raw.function.name
    try:
        parsed = json.loads(raw.function.arguments or "{}")
    except json.JSONDecodeError as exc:
        return ToolCall(id=raw.id, name=name, parse_error=f"arguments are not valid JSON: {exc}")
    if not isinstance(parsed, dict):
        return ToolCall(id=raw.id, name=name, parse_error="arguments must be a JSON object")
    return ToolCall(id=raw.id, name=name, arguments=parsed)


@dataclass
class LiteLLMModel:
    """Any provider LiteLLM supports, named `provider/model`. Keys come from env vars."""

    name: str
    temperature: float | None = None
    seed: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)
    track_cost: bool = True
    sleep: Callable[[float], None] = time.sleep

    def complete(self, messages: Sequence[Message], tools: Sequence[ToolSpec]) -> Reply:
        """Call the provider and translate its reply. Failures raise a redacted ModelError."""
        litellm = _load_litellm()
        params: dict[str, Any] = {
            "model": self.name,
            "messages": [_to_wire(m) for m in messages],
            # Drop params a provider doesn't support (e.g. seed) instead of failing the call.
            "drop_params": True,
            **self.extra,
        }
        if tools:
            params["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.parameters,
                    },
                }
                for t in tools
            ]
        if self.temperature is not None:
            params["temperature"] = self.temperature
        if self.seed is not None:
            params["seed"] = self.seed
        response = self._call_with_retries(litellm, params)
        message = response.choices[0].message
        usage = getattr(response, "usage", None)
        input_tokens = getattr(usage, "prompt_tokens", 0) or 0
        output_tokens = getattr(usage, "completion_tokens", 0) or 0
        return Reply(
            text=message.content or "",
            tool_calls=tuple(_from_wire(c) for c in message.tool_calls or ()),
            usage=Usage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_usd=self._cost(litellm, response, input_tokens + output_tokens),
            ),
        )

    def _call_with_retries(self, litellm: Any, params: dict[str, Any]) -> Any:
        delays = iter(RETRY_DELAYS)
        while True:
            try:
                return litellm.completion(**params)
            except Exception as exc:
                delay = next(delays, None) if _is_temporary(exc) else None
                if delay is None:
                    # "from None": a chained provider exception could carry request headers.
                    detail = redact_text(str(exc))
                    raise ModelError(f"{self.name}: {type(exc).__name__}: {detail}") from None
            self.sleep(delay)

    def _cost(self, litellm: Any, response: Any, tokens: int) -> float | None:
        """Prefer the price the provider reported, since LiteLLM's estimate can be wrongly 0."""
        if not self.track_cost:
            return None
        hidden = getattr(response, "_hidden_params", None) or {}
        reported = hidden.get("response_cost")
        if reported is None:
            reported = getattr(getattr(response, "usage", None), "cost", None)
        if reported is None:
            try:
                reported = litellm.completion_cost(completion_response=response)
            except Exception:
                return None  # An unpriced model (e.g. a brand-new or local one) is not an error.
        cost = float(reported)
        # A call that used tokens wasn't free: the price is unknown, so don't report it as $0.
        return None if cost == 0.0 and tokens else cost
