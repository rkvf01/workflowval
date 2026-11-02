"""One real provider call, to prove the adapter end to end. Run with: uv run pytest -m live"""

from __future__ import annotations

import os

import pytest

from workflowval.models import LiteLLMModel, Message, ToolSpec

pytestmark = [pytest.mark.live, pytest.mark.enable_socket]

MODEL = os.environ.get("WORKFLOWVAL_LIVE_MODEL", "openrouter/vendor/model")

ADD = ToolSpec(
    "add",
    "Add two integers.",
    {
        "type": "object",
        "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
        "required": ["a", "b"],
    },
)


@pytest.mark.skipif(
    MODEL.startswith("anthropic/") and not os.environ.get("ANTHROPIC_API_KEY"),
    reason="ANTHROPIC_API_KEY is not set",
)
def test_a_real_model_can_call_a_tool() -> None:
    reply = LiteLLMModel(MODEL).complete(
        [Message("user", "Use the add tool to add 2 and 3.")],
        [ADD],
    )
    assert reply.tool_calls
    assert reply.tool_calls[0].name == "add"
    assert reply.tool_calls[0].arguments == {"a": 2, "b": 3}
    assert reply.usage.input_tokens > 0
    assert reply.usage.output_tokens > 0
