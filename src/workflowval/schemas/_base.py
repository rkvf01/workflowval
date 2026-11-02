"""Shared base model and JSON file helpers for every data format."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel, ConfigDict

T = TypeVar("T", bound=BaseModel)


class Strict(BaseModel):
    """Base for on-disk formats: unknown fields are errors, so typos fail loudly."""

    model_config = ConfigDict(extra="forbid")


def atomic_write_text(path: Path, text: str) -> None:
    """Write text so that a killed process never leaves a half-written file behind."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def save_json(model: BaseModel, path: Path) -> None:
    """Write a model as indented JSON, atomically."""
    payload = json.loads(model.model_dump_json(exclude_none=True))
    atomic_write_text(path, json.dumps(payload, indent=2) + "\n")


def load_json(cls: type[T], path: Path) -> T:
    """Read and validate a JSON file. Raises pydantic.ValidationError on bad data."""
    return cls.model_validate_json(path.read_text(encoding="utf-8"))
