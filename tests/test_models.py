from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import litellm
import pytest

from workflowval.models import (
    FakeModel,
    FakeScriptExhausted,
    LiteLLMModel,
    Message,
    ModelError,
    Reply,
    ToolCall,
    ToolSpec,
    Usage,
    call,
)
from workflowval.models.litellm_adapter import RETRY_DELAYS

SPEC = ToolSpec("read_spec", "Read it.", {"type": "object", "properties": {}})


def test_fake_replays_in_order_and_records_requests() -> None:
    fake = FakeModel([Reply(text="one"), call("read_spec", operation_id="x")])
    assert fake.complete([Message("user", "hi")], [SPEC]).text == "one"
    second = fake.complete([], [])
    assert second.tool_calls[0].arguments == {"operation_id": "x"}
    assert fake.requests[0] == ([Message("user", "hi")], [SPEC])


def test_fake_fails_clearly_when_the_script_runs_out() -> None:
    fake = FakeModel([Reply()])
    fake.complete([], [])
    with pytest.raises(FakeScriptExhausted, match="after 1 replies"):
        fake.complete([], [])


def test_fake_loads_a_script_from_a_file(tmp_path: Path) -> None:
    path = tmp_path / "script.json"
    script = [
        {"text": "hi", "usage": {"input_tokens": 3, "output_tokens": 4, "cost_usd": 0.5}},
        {"tool_calls": [{"id": "1", "name": "read_spec", "arguments": {"a": 1}}]},
    ]
    path.write_text(json.dumps(script))
    fake = FakeModel.from_file(path)
    assert fake.complete([], []).usage == Usage(3, 4, 0.5)
    assert fake.complete([], []).tool_calls == (ToolCall("1", "read_spec", {"a": 1}),)


def wire_response(content: str | None = "ok", tool_calls: list[Any] | None = None) -> Any:
    message = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message)],
        usage=SimpleNamespace(prompt_tokens=11, completion_tokens=7),
    )


def wire_call(arguments: str) -> Any:
    return SimpleNamespace(id="c1", function=SimpleNamespace(name="read_spec", arguments=arguments))


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    captured: dict[str, Any] = {"response": wire_response()}

    def completion(**params: Any) -> Any:
        captured["params"] = params
        return captured["response"]

    monkeypatch.setattr(litellm, "completion", completion)
    monkeypatch.setattr(litellm, "completion_cost", lambda completion_response: 0.25)
    return captured


def test_adapter_sends_provider_neutral_requests(sent: dict[str, Any]) -> None:
    earlier = ToolCall("c0", "read_spec", {"operation_id": "x"})
    LiteLLMModel("anthropic/some-model", temperature=0.5, seed=7, extra={"top_p": 0.9}).complete(
        [
            Message("system", "be good"),
            Message("assistant", "", tool_calls=(earlier,)),
            Message("tool", "result", tool_call_id="c0"),
        ],
        [SPEC],
    )
    params = sent["params"]
    assert params["model"] == "anthropic/some-model"
    assert (params["temperature"], params["seed"], params["top_p"]) == (0.5, 7, 0.9)
    assert params["drop_params"] is True
    assert params["tools"][0]["function"]["name"] == "read_spec"
    assert params["messages"][1]["tool_calls"][0]["function"] == {
        "name": "read_spec",
        "arguments": '{"operation_id": "x"}',
    }
    assert params["messages"][2]["tool_call_id"] == "c0"


def test_adapter_leaves_out_unset_options(sent: dict[str, Any]) -> None:
    LiteLLMModel("ollama/x").complete([Message("user", "hi")], [])
    assert not {"temperature", "seed", "tools"} & set(sent["params"])


def test_adapter_maps_reply_usage_and_cost(sent: dict[str, Any]) -> None:
    sent["response"] = wire_response(None, [wire_call('{"operation_id": "x"}')])
    reply = LiteLLMModel("a/b").complete([], [])
    assert reply.text == ""
    assert reply.tool_calls == (ToolCall("c1", "read_spec", {"operation_id": "x"}),)
    assert reply.usage == Usage(11, 7, 0.25)


@pytest.mark.parametrize(
    ("arguments", "message"), [("{not json", "not valid JSON"), ("[1]", "JSON object")]
)
def test_adapter_reports_malformed_arguments_rather_than_crashing(
    sent: dict[str, Any], arguments: str, message: str
) -> None:
    sent["response"] = wire_response(None, [wire_call(arguments)])
    tool_call = LiteLLMModel("a/b").complete([], []).tool_calls[0]
    assert tool_call.parse_error is not None
    assert message in tool_call.parse_error


def test_adapter_treats_empty_arguments_as_no_arguments(sent: dict[str, Any]) -> None:
    sent["response"] = wire_response(None, [wire_call("")])
    assert LiteLLMModel("a/b").complete([], []).tool_calls[0].arguments == {}


def test_adapter_tolerates_a_model_with_no_known_price(
    sent: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def unpriced(completion_response: Any) -> float:
        raise ValueError("model isn't mapped")

    monkeypatch.setattr(litellm, "completion_cost", unpriced)
    usage = LiteLLMModel("a/b").complete([], []).usage
    assert usage.cost_usd is None
    assert usage.input_tokens == 11


def test_the_price_the_provider_reported_wins(
    sent: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    # LiteLLM's own estimate returned 0 for real OpenRouter calls, so it must not take priority.
    monkeypatch.setattr(litellm, "completion_cost", lambda completion_response: 0.0)
    response = wire_response()
    response._hidden_params = {"response_cost": 0.003}
    sent["response"] = response
    assert LiteLLMModel("a/b").complete([], []).usage.cost_usd == 0.003


def test_cost_can_come_from_the_usage_block(
    sent: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(litellm, "completion_cost", lambda completion_response: 0.0)
    response = wire_response()
    response.usage.cost = 0.002
    sent["response"] = response
    assert LiteLLMModel("a/b").complete([], []).usage.cost_usd == 0.002


def test_zero_cost_with_tokens_is_unknown_not_free(
    sent: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(litellm, "completion_cost", lambda completion_response: 0.0)
    assert LiteLLMModel("a/b").complete([], []).usage.cost_usd is None


def test_cost_tracking_can_be_turned_off(sent: dict[str, Any]) -> None:
    usage = LiteLLMModel("a/b", track_cost=False).complete([], []).usage
    assert usage.cost_usd is None
    assert usage.input_tokens == 11


def test_provider_errors_are_wrapped_and_redacted(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(**_: Any) -> Any:
        raise RuntimeError("401 for Authorization: Bearer abc123def456")

    monkeypatch.setattr(litellm, "completion", fail)
    with pytest.raises(ModelError) as caught:
        LiteLLMModel("a/b").complete([], [])
    assert "abc123def456" not in str(caught.value)
    assert "RuntimeError" in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__


class Provider(Exception):
    def __init__(self, status_code: int | None = None) -> None:
        super().__init__("provider said no")
        if status_code is not None:
            self.status_code = status_code


class RateLimitError(Exception):
    pass


@pytest.mark.parametrize(
    "error", [Provider(408), Provider(429), Provider(503), RateLimitError("slow down")]
)
def test_a_temporary_provider_error_is_tried_again(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    replies: list[Any] = [error, error, wire_response("back")]

    def answer(**_: Any) -> Any:
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(litellm, "completion", answer)
    waits: list[float] = []
    assert LiteLLMModel("a/b", sleep=waits.append).complete([], []).text == "back"
    assert waits == [2.0, 4.0]


def test_a_lasting_rate_limit_ends_with_a_model_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(**_: Any) -> Any:
        raise Provider(429)

    monkeypatch.setattr(litellm, "completion", fail)
    waits: list[float] = []
    with pytest.raises(ModelError, match="Provider"):
        LiteLLMModel("a/b", sleep=waits.append).complete([], [])
    assert waits == list(RETRY_DELAYS)


@pytest.mark.parametrize("status", [400, 401, 404])
def test_a_request_the_provider_rejects_is_not_tried_again(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    def fail(**_: Any) -> Any:
        raise Provider(status)

    monkeypatch.setattr(litellm, "completion", fail)
    waits: list[float] = []
    with pytest.raises(ModelError):
        LiteLLMModel("a/b", sleep=waits.append).complete([], [])
    assert waits == []


def test_adapter_handles_real_litellm_objects_offline() -> None:
    # mock_response goes through LiteLLM's own request and response code without any network.
    tool_calls = [
        {"id": "c1", "type": "function", "function": {"name": "read_spec", "arguments": '{"a": 1}'}}
    ]
    model = LiteLLMModel(
        "openrouter/vendor/model",
        temperature=0.0,
        seed=1,
        extra={"mock_response": "hello", "mock_tool_calls": tool_calls, "api_key": "unused"},
    )
    reply = model.complete(
        [
            Message("system", "s"),
            Message("user", "u"),
            Message("assistant", "", tool_calls=(ToolCall("c0", "read_spec", {"a": 0}),)),
            Message("tool", "r", tool_call_id="c0"),
        ],
        [SPEC],
    )
    assert reply.text == "hello"
    assert reply.tool_calls == (ToolCall("c1", "read_spec", {"a": 1}),)
    assert reply.usage.input_tokens > 0
    assert reply.usage.output_tokens > 0
