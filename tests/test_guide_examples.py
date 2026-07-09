"""The developer's guide stays runnable (guide/README.md).

Each guide script is a self-checking program (its asserts ARE its test); this
module executes every one so the guide can never drift from the code it
teaches — the same promise the spec fixtures make.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

GUIDE = Path(__file__).resolve().parents[1] / "guide"
SCRIPTS = sorted(GUIDE.glob("0*_*.py"))


def _run(path: Path) -> None:
    spec = importlib.util.spec_from_file_location(f"guide_{path.stem}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        module.main()
    finally:
        sys.modules.pop(spec.name, None)


@pytest.mark.parametrize("script", SCRIPTS, ids=[p.stem for p in SCRIPTS])
def test_guide_script_runs(script: Path) -> None:
    _run(script)


def test_the_guide_ships_all_five_scripts() -> None:
    assert [p.stem[:2] for p in SCRIPTS] == ["01", "02", "03", "04", "05"]
