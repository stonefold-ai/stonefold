# SPDX-License-Identifier: Apache-2.0
"""CLI: draft a registry (and its handler stubs) from what an integrator has.

Usage (from the repo checkout)::

    python -m stonefold_registry_gen sql     schema.sql   --domain payments -o draft.registry.yaml
    python -m stonefold_registry_gen openapi api.yaml     --domain ledger
    python -m stonefold_registry_gen mcp     tools.json   --domain crm
    # draft the registry AND the connector/scope-predicate code stubs (G1):
    python -m stonefold_registry_gen sql     schema.sql   --domain payments --stubs handlers.py
    # stubs for every declared name in an existing authoring registry:
    python -m stonefold_registry_gen stubs   payments.registry.yaml -o handlers.py

The output is a DRAFT: every guessed kind/attribute carries a TODO(review) marker
and every generated handler raises NotImplementedError (fail closed until a human
completes it). Drafts are validated before they are written (registry -> the JSON
schema; stubs -> Python syntax); a validation failure is a generator bug, exit 1.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import yaml

from stonefold_registry_gen.emit import emit_yaml, validate_registry_yaml
from stonefold_registry_gen.importers import draft_from_mcp_tools, draft_from_openapi
from stonefold_registry_gen.model import DraftRegistry
from stonefold_registry_gen.sql import draft_from_sql
from stonefold_registry_gen.stubs import (
    emit_stubs,
    plan_from_draft,
    plan_from_registry,
    validate_stub_code,
)


def _load_structured(path: Path) -> Any:
    # YAML is a JSON superset, so one loader covers .json/.yaml/.yml inputs.
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _build(source: str, path: Path, domain: str) -> DraftRegistry:
    if source == "sql":
        return draft_from_sql(path.read_text(encoding="utf-8"), domain=domain)
    if source == "openapi":
        return draft_from_openapi(_load_structured(path), domain=domain)
    return draft_from_mcp_tools(_load_structured(path), domain=domain)


def _emit_stub_file(text: str, out: Path | None, *, what: str) -> int:
    """Validate emitted stub code and write it (or print to stdout)."""
    problems = validate_stub_code(text)
    if problems:  # a generator bug — never ship an unparsable stub
        for p in problems:
            print(f"error: {p}", file=sys.stderr)
        return 1
    if out is None:
        sys.stdout.write(text)
    else:
        out.write_text(text, encoding="utf-8", newline="\n")
        print(f"wrote {out} ({what}) — implement every NotImplementedError before use",
              file=sys.stderr)
    return 0


def _run_stubs_from_registry(input_path: Path, out: Path | None) -> int:
    doc = _load_structured(input_path)
    if not isinstance(doc, dict):
        print(f"error: {input_path} is not a registry document", file=sys.stderr)
        return 1
    plan = plan_from_registry(doc)
    return _emit_stub_file(emit_stubs(plan), out, what="handler stubs")


def _run_draft(source: str, input_path: Path, domain: str, out: Path | None,
               stubs_out: Path | None) -> int:
    draft = _build(source, input_path, domain)
    if not draft.entities:
        print(f"error: no entities/actions found in {input_path}", file=sys.stderr)
        return 1

    text = emit_yaml(draft)
    problems = validate_registry_yaml(text)
    if problems:  # a generator bug — never ship an invalid draft
        for p in problems:
            print(f"error: generated draft fails registry.schema.json: {p}", file=sys.stderr)
        return 1

    if out is None:
        sys.stdout.write(text)
    else:
        entities = len(draft.entities)
        actions = sum(len(e.actions) for e in draft.entities)
        out.write_text(text, encoding="utf-8", newline="\n")
        print(
            f"wrote {out} ({entities} entities, {actions} drafted actions) — "
            f"review every TODO(review) before use",
            file=sys.stderr,
        )

    if stubs_out is not None:  # G1: also draft the handler code
        return _emit_stub_file(emit_stubs(plan_from_draft(draft)), stubs_out, what="handler stubs")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="stonefold_registry_gen",
        description="Draft a Stonefold registry + handler stubs (authoring format, docs/06) "
                    "from existing artefacts.",
    )
    parser.add_argument("source", choices=("sql", "openapi", "mcp", "stubs"),
                        help="input artefact type ('stubs' reads an authoring registry)")
    parser.add_argument("input", type=Path,
                        help="DDL / OpenAPI / MCP tool list, or (for 'stubs') a registry")
    parser.add_argument("--domain", default=None, help="registry domain (default: input file stem)")
    parser.add_argument("-o", "--out", type=Path, default=None, help="output file (default: stdout)")
    parser.add_argument("--stubs", type=Path, default=None,
                        help="also draft connector/scope-predicate code stubs to this file (G1)")
    args = parser.parse_args(argv)

    if args.source == "stubs":
        return _run_stubs_from_registry(args.input, args.out)

    domain = args.domain or args.input.stem
    return _run_draft(args.source, args.input, domain, args.out, args.stubs)


if __name__ == "__main__":
    raise SystemExit(main())
