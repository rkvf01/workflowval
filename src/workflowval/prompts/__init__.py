"""Versioned prompt files. Prompts are never inline strings."""

from __future__ import annotations

import re
from importlib import resources

_HEADER = re.compile(r"\A<!-- version: (\d+) -->\n")


def load_prompt(name: str) -> str:
    """Return a prompt's text without its required version header."""
    text = resources.files(__package__).joinpath(f"{name}.md").read_text(encoding="utf-8")
    match = _HEADER.match(text)
    if match is None:
        raise ValueError(f"prompt {name!r} has no '<!-- version: N -->' header on its first line")
    return text[match.end() :].strip()


def render_prompt(name: str, **values: str) -> str:
    """Return a prompt with its <<name>> slots filled. Values are inserted as plain data."""
    text = load_prompt(name)
    for key, value in values.items():
        text = text.replace(f"<<{key}>>", value)
    return text


def prompt_version(name: str) -> int:
    """Return a prompt's version number."""
    text = resources.files(__package__).joinpath(f"{name}.md").read_text(encoding="utf-8")
    match = _HEADER.match(text)
    if match is None:
        raise ValueError(f"prompt {name!r} has no '<!-- version: N -->' header on its first line")
    return int(match.group(1))
