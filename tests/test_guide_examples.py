"""The developer's guide stays runnable (guide/README.md).

Each tutorial directory ships a ``main.py`` that starts the REAL gateway
service (a uvicorn subprocess on a local port, except 01 which teaches the
in-process internals) and drives the agent/operator files against it over
HTTP, asserting as it goes. This module executes every one, so the guide can
never drift from the code it teaches — the same promise the spec fixtures
make.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

GUIDE = Path(__file__).resolve().parents[1] / "guide"
EXAMPLES = sorted(p for p in GUIDE.iterdir() if p.is_dir() and (p / "main.py").exists())


def _run(example_dir: Path) -> None:
    main_py = example_dir / "main.py"
    spec = importlib.util.spec_from_file_location(f"guide_{example_dir.name}", main_py)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    # running ``python guide/<x>/main.py`` puts the example dir on sys.path;
    # reproduce that so sibling files (agent.py, functions.py, ...) import.
    sys.path.insert(0, str(example_dir))
    try:
        spec.loader.exec_module(module)
        module.main()
    finally:
        sys.path.remove(str(example_dir))
        sys.modules.pop(spec.name, None)
        # sibling modules cached under generic names must not leak between
        # examples (each dir has its own agent.py / gateway_service.py):
        for name in ("agent", "functions", "gateway_service", "operator_console",
                     "erp_adapter"):
            sys.modules.pop(name, None)


@pytest.mark.parametrize("example", EXAMPLES, ids=[p.name for p in EXAMPLES])
def test_guide_example_runs(example: Path) -> None:
    _run(example)


def test_the_guide_ships_all_five_tutorials() -> None:
    assert [p.name[:2] for p in EXAMPLES] == ["01", "02", "03", "04", "05"]
