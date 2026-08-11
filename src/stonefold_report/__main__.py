# SPDX-License-Identifier: Apache-2.0
"""Produce an Advisory Report from an exported audit log.

    python -m stonefold_report audit.jsonl --agent ap-desk -o report.html

One JSON audit record per line, as ``AuditRecord`` serialises them. Optional
inputs, both of which the report is honest about lacking:

    --verdicts verdicts.json     {"gate:denylist": "legitimate", ...}
    --downstream refused.txt     one correlation id per line

Without ``--verdicts`` the report states no false-positive rate. Without
``--downstream`` it makes no exclusivity claim. Neither absence is an error: a
missing input is reported as missing, because the alternative is a zero that
reads like a finding.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from stonefold_core.models import AuditRecord
from stonefold_report.figures import MixedDatasetError, NotAdvisoryError, build_report
from stonefold_report.html import render_html
from stonefold_report.render import render


def _load(path: Path) -> list[AuditRecord]:
    records = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            records.append(AuditRecord.model_validate_json(line))
        except Exception as exc:  # a malformed line is a hole in the evidence
            raise SystemExit(f"{path}:{number}: not a valid audit record: {exc}")
    return records


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="stonefold_report", description=__doc__)
    parser.add_argument("audit", type=Path, help="audit log, one JSON record per line")
    parser.add_argument("--agent", required=True, help="the agent to report on")
    parser.add_argument("-o", "--out", type=Path, help="write here instead of stdout")
    parser.add_argument(
        "--format", choices=("md", "html"), default=None,
        help="output form; inferred from -o's suffix, else markdown. The HTML "
             "edition is the customer-facing page; the Markdown edition carries "
             "the reproduction queries. Ship both.",
    )
    parser.add_argument(
        "--verdicts", type=Path,
        help="customer rulings, JSON object keyed '<rule> @ <Resource>.<action>' "
             "with values legitimate|correct|unsure",
    )
    parser.add_argument("--downstream", type=Path, help="correlation ids also refused")
    parser.add_argument(
        "--prepared-for", metavar="NAME",
        help="addressee for the letterhead; adds a confidentiality line",
    )
    args = parser.parse_args(argv)

    verdicts = (
        json.loads(args.verdicts.read_text(encoding="utf-8"))
        if args.verdicts is not None
        else None
    )
    downstream = (
        [ln.strip() for ln in args.downstream.read_text(encoding="utf-8").splitlines() if ln.strip()]
        if args.downstream is not None
        else None
    )
    try:
        report = build_report(
            _load(args.audit),
            agent=args.agent,
            verdicts=verdicts,
            downstream_refused=downstream,
        )
    except (MixedDatasetError, NotAdvisoryError) as exc:
        # Refusing to report is a result. It is printed like one.
        print(f"no report produced: {exc}", file=sys.stderr)
        return 2
    form = args.format or (
        "html" if args.out is not None and args.out.suffix == ".html" else "md"
    )
    from datetime import datetime, timezone

    stamp = datetime.now(timezone.utc)
    text = (
        render_html(report, prepared_for=args.prepared_for, generated_at=stamp)
        if form == "html"
        else render(report, prepared_for=args.prepared_for, generated_at=stamp)
    )
    if args.out is not None:
        args.out.write_text(text, encoding="utf-8", newline="\n")
        print(f"wrote {args.out}", file=sys.stderr)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
