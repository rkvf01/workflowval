"""Shared test setup. Sockets are blocked by pytest-socket (see pyproject.toml)."""

from __future__ import annotations

import os

os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
