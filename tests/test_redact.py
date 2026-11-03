from __future__ import annotations

import json

import pytest

from workflowval.redact import MASK, redact, redact_text


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        ("Authorization: Bearer abc123def456", "abc123def456"),
        ('{"Authorization": "Basic dXNlcjpwYXNz"}', "dXNlcjpwYXNz"),
        ("got bearer abc.def-ghi back", "abc.def-ghi"),
        ("Cookie: token=abc123def456", "abc123def456"),
        ("GET /pets?api_key=abc123def456&limit=1", "abc123def456"),
        ('{"token": "abc123def456"}', "abc123def456"),
        ("x-api-key: abc123def456", "abc123def456"),
        ("failed with sk-test-abc123def456", "abc123def456"),
    ],
)
def test_common_secret_shapes_are_masked(text: str, secret: str) -> None:
    out = redact_text(text)
    assert secret not in out
    assert MASK in out


def test_other_text_survives() -> None:
    text = "GET /pets?limit=1 returned 200 with 5 max_tokens"
    assert redact_text(text) == text


def test_explicit_secrets_are_masked_longest_first() -> None:
    assert redact_text("a hunter2hunter2 b", ["hunter2", "hunter2hunter2"]) == f"a {MASK} b"


def test_values_of_secret_env_vars_are_masked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MY_SERVICE_API_KEY", "plain-looking-value")
    monkeypatch.setenv("SHORT_TOKEN", "abc")
    monkeypatch.setenv("UNRELATED", "visible-value")
    out = redact_text("plain-looking-value visible-value abc")
    assert out == f"{MASK} visible-value abc"


def test_containers_are_walked_by_key_and_value() -> None:
    out = redact(
        {
            "headers": {"Authorization": "anything", "Accept": "json"},
            "password": "pw",
            "input_tokens": 5,
            "token_env": "BOOKER_TOKEN",
            "notes": ["Bearer abc123def456", 3, ("Cookie: a=b",)],
        }
    )
    assert out == {
        "headers": {"Authorization": MASK, "Accept": "json"},
        "password": MASK,
        "input_tokens": 5,
        "token_env": "BOOKER_TOKEN",
        "notes": [MASK, 3, [f"Cookie: {MASK}"]],
    }


def test_a_json_body_stays_valid_and_keeps_its_error_message() -> None:
    body = '{"errors": {"token": ["is missing"]}}'
    assert json.loads(redact(body)) == {"errors": {"token": ["is missing"]}}
    assert redact_text(body) == body


def test_a_token_in_a_json_body_is_hidden_but_the_shape_stays() -> None:
    out = json.loads(redact('{"user": {"token": "abc123def456", "bio": null, "name": "ann"}}'))
    assert out == {"user": {"token": MASK, "bio": None, "name": "ann"}}


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        (True, True),
        (["abc123def456"], [MASK]),
        (["is missing"], ["is missing"]),
        ({"value": "abc123def456"}, {"value": MASK}),
        (12345, MASK),
    ],
)
def test_a_secret_field_keeps_its_shape(value: object, expected: object) -> None:
    assert redact({"token": value}) == {"token": expected}


def test_a_broken_json_body_is_still_redacted_as_text() -> None:
    out = redact('{"token": "abc123def456"')
    assert isinstance(out, str)
    assert "abc123def456" not in out
