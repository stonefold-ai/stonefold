"""Structured output I/O for both tracks (docs/15 §5).

Three formats, one contract:

* **Trials (JSONL)** — one JSON object per line, *appended and flushed as each trial
  completes* (``JsonlWriter``), so a run cut short still leaves every finished trial
  on disk, and a reader can tail the file while the run is live.
* **Cells (JSON + CSV)** — the aggregated per-cell rates, rewritten after every
  completed round; the graph-ready artifact (one row per cell, plain columns).
* **Anything (JSON)** — ``write_json`` for run metadata and self-describing bundles.

The raw trial log is the published artifact (§5/§6): a reviewer recomputes the matrix
from it; the cells files are conveniences derived from it.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from types import TracebackType
from typing import Any

from stonefold_bench.runner import Trial


class JsonlWriter:
    """Append one JSON object per line, flushed after every write.

    The flush is the point: the caller hands one dict per finished trial, and that
    trial is durable on disk *immediately* — not when the run ends. Use as a context
    manager so the handle closes even when the run raises.
    """

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.count = 0
        self._fh = path.open("w", encoding="utf-8")

    def write(self, obj: dict[str, Any]) -> None:
        self._fh.write(json.dumps(obj, sort_keys=True) + "\n")
        self._fh.flush()
        self.count += 1

    def close(self) -> None:
        self._fh.close()

    def __enter__(self) -> "JsonlWriter":
        return self

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None,
                 tb: TracebackType | None) -> None:
        self.close()


def write_jsonl(path: Path, trials: list[Trial]) -> Path:
    """One-shot write of a finished trial list (kept for batch use; the incremental
    path is ``JsonlWriter``). Returns the path."""
    with JsonlWriter(path) as writer:
        for t in trials:
            writer.write(t.as_dict())
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


def write_json(path: Path, obj: dict[str, Any]) -> Path:
    """Write one JSON document (cells bundle, run metadata). Stable key order."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def write_csv(path: Path, rows: list[dict[str, Any]],
              *, fieldnames: list[str] | None = None) -> Path:
    """Write flat dict rows as CSV — the spreadsheet/graphing convenience next to the
    JSON. Column order: ``fieldnames`` if given, else the first row's key order."""
    path.parent.mkdir(parents=True, exist_ok=True)
    names = fieldnames if fieldnames is not None else (list(rows[0].keys()) if rows else [])
    with path.open("w", encoding="utf-8", newline="") as fh:
        if not names:
            return path
        writer = csv.DictWriter(fh, fieldnames=names)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return path
