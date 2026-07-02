"""Raw-log I/O (docs/15 §5): every trial is one JSONL line, published verbatim so a
gateway vendor can rerun the harness and recompute the matrix from the logs.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from acp_bench.runner import Trial


def write_jsonl(path: Path, trials: list[Trial]) -> Path:
    """Append-free write of one JSON object per line. Returns the path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for t in trials:
            fh.write(json.dumps(t.as_dict(), sort_keys=True) + "\n")
    return path


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Read a raw log back into plain dicts (for recomputation / inspection)."""
    out: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out
