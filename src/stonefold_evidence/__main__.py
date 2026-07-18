# SPDX-License-Identifier: Apache-2.0
"""CLI: export an audit evidence pack (implementation-plan Workstream G3).

    python -m stonefold_evidence --jsonl audit.jsonl --policy examples/payments-ops.stele.yaml -o pack.md
    python -m stonefold_evidence --postgres "postgresql://stonefold@localhost/stonefold" -o pack.md

Read-only over the audit store; writes a Markdown report keyed to the docs/14 controls.
Every regulatory `[VERIFY]` marker is printed verbatim — the citations are the author's
to verify before the pack is relied on.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

from stonefold_core.models import AuditRecord

from stonefold_evidence.pack import build_evidence_pack
from stonefold_evidence.render import render_markdown
from stonefold_evidence.sources import records_from_jsonl, records_from_postgres


def _utf8_stdout() -> None:
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        try:
            reconfigure(encoding="utf-8")
        except (ValueError, OSError):
            pass


def main(argv: list[str] | None = None) -> int:
    _utf8_stdout()
    parser = argparse.ArgumentParser(
        prog="stonefold_evidence",
        description="Export an audit evidence pack (docs/14 controls) — read-only.",
    )
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--jsonl", type=Path, help="a JSONL audit export (one record/line)")
    src.add_argument("--postgres", metavar="DSN", help="a Postgres DSN with an audit_log table")
    parser.add_argument("--table", default="audit_log", help="audit table name (postgres)")
    parser.add_argument("--policy", default=None, help="the policy file path (the documented control, Art. 26)")
    parser.add_argument("-o", "--out", type=Path, default=None, help="output .md (default: stdout)")
    args = parser.parse_args(argv)

    records: list[AuditRecord]
    if args.jsonl is not None:
        records = records_from_jsonl(args.jsonl)
    else:
        import psycopg  # lazy: only the postgres path needs it

        conn = psycopg.connect(args.postgres)
        try:
            records = records_from_postgres(conn, table=args.table)
        finally:
            conn.close()

    pack = build_evidence_pack(
        records, policy_ref=args.policy, generated_at=datetime.now(timezone.utc)
    )
    text = render_markdown(pack)

    if args.out is None:
        sys.stdout.write(text)
    else:
        args.out.write_text(text, encoding="utf-8", newline="\n")
        print(f"wrote {args.out} ({pack.total_records} records over "
              f"{len(pack.controls)} controls) — verify every [VERIFY] before use",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
