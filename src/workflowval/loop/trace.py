"""`.workflowval/trace.jsonl`: every model and tool call (NFR-6), with secrets removed (NFR-7)."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from workflowval.redact import redact


class Tracer:
    """Appends one JSON object per event, flushed at once so a killed run keeps its trace."""

    def __init__(self, path: Path, *, echo: bool = False) -> None:
        self.path = path
        self.echo = echo
        path.parent.mkdir(parents=True, exist_ok=True)

    def event(self, kind: str, **fields: Any) -> None:
        """Record one event."""
        record = {"ts": datetime.now(UTC).isoformat(), "kind": kind, **redact(fields)}
        line = json.dumps(record, default=str)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
        if self.echo:
            print(line, file=sys.stderr)
