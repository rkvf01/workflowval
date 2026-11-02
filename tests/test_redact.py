from __future__ import annotations

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
