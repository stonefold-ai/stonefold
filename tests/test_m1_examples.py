"""M1 — every valid example loads & validates (Acceptance A5).

Each example is schema-validated against schema/acp.schema.json and passes the
semantic linter with no ERROR findings (warnings are allowed).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from acp_core import load_policy
from tests.conftest import full_registry, load_schema, load_yaml, valid_example_paths


@pytest.mark.parametrize(
    "path", valid_example_paths(), ids=lambda p: p.name
)
def test_a5_example_loads_and_validates(path: Path) -> None:
    data = load_yaml(path)
    compiled = load_policy(data, full_registry(), schema=load_schema())
    assert compiled.lint_report is not None
    errors = compiled.lint_report.errors
    assert not errors, f"{path.name} has lint errors:\n" + "\n".join(
        e.message for e in errors
    )


def test_all_six_examples_present() -> None:
    names = {p.name for p in valid_example_paths()}
    assert names == {
        "support-assistant.acp.yaml",
        "ward-nurse.acp.yaml",
        "track-operator.acp.yaml",
        "payments-ops.acp.yaml",
        "legal-matter.acp.yaml",
        "vehicle-controller.acp.yaml",
    }
