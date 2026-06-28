# ACP Gateway — developer tasks. See docs/03 for the pinned stack.
#
# `make demo` is the M-DEMO milestone: the adversarial demo end to end.
# On Windows without `make`, run the same thing with:  python -m acp_demo

# Prefer the project venv if present, else the active interpreter.
ifeq ($(OS),Windows_NT)
  VENV_PY := .venv/Scripts/python.exe
else
  VENV_PY := .venv/bin/python
endif
PYTHON := $(if $(wildcard $(VENV_PY)),$(VENV_PY),python)

.PHONY: demo test test-unit typecheck examples

demo:               ## run the adversarial demo (G1 injection, G2 kill, G3 invite-attack)
	$(PYTHON) -m acp_demo

test:               ## full suite (needs Docker for the integration tests)
	$(PYTHON) -m pytest -q

test-unit:          ## fast unit suite (no Docker)
	$(PYTHON) -m pytest -q -m "not integration"

typecheck:          ## mypy --strict
	$(PYTHON) -m mypy

examples:           ## load + validate every example policy
	$(PYTHON) -m pytest -q -k example
