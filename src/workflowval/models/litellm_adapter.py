"""The default adapter. This is the only module that imports a provider library (NFR-5)."""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from workflowval.models.base import Message, ModelError, Reply, ToolCall, ToolSpec, Usage
from workflowval.redact import redact_text


def _load_litellm() -> Any:
    # Without this, importing litellm fetches its price list from the network.
    os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    import litellm

    return litellm


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
        try:
            response = litellm.completion(**params)
        except Exception as exc:
            # "from None": a chained provider exception could carry request headers.
            detail = redact_text(str(exc))
            raise ModelError(f"{self.name}: {type(exc).__name__}: {detail}") from None
        message = response.choices[0].message
        usage = getattr(response, "usage", None)
        return Reply(
            text=message.content or "",
            tool_calls=tuple(_from_wire(c) for c in message.tool_calls or ()),
            usage=Usage(
                input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                output_tokens=getattr(usage, "completion_tokens", 0) or 0,
                cost_usd=self._cost(litellm, response),
            ),
        )

    @staticmethod
    def _cost(litellm: Any, response: Any) -> float | None:
        try:
            return float(litellm.completion_cost(completion_response=response))
        except Exception:
            return None  # An unpriced model (e.g. a brand-new or local one) is not an error.
