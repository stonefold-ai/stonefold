# Contributing to Stonefold

Thanks for your interest. This repo is the **Python reference implementation** of the
Stonefold gateway, plus the runnable TCK, demos, and benchmarks. The specifications'
canonical home is [stonefold-ai/spec](https://github.com/stonefold-ai/spec) — spec defects
and ambiguities belong there; implementation issues belong here.

## Ground rules (the non-negotiables)

The invariants in [`CLAUDE.md`](CLAUDE.md) bind every change; a violation is treated as a
P0 bug. The short list: deterministic enforcement (no LLM in `enforce()`), default deny
and deny-wins, identity/scope injected below the model, effects staged through the outbox,
the kill no-race transaction, transactional audit, fail closed, and the frozen vocabulary
(no new kinds/gates/operators — extensions go in registries and hooks).

## Development setup

```
git clone --recurse-submodules https://github.com/stonefold-ai/stonefold.git
                               # the spec/ submodule carries the schemas + fixtures
pip install -e ".[dev]"        # Python 3.11+
docker compose up -d           # Postgres + Redis (integration tests / local runs)
pytest -q -m "not integration" # fast suite
pytest -q                      # full suite (testcontainers needs Docker running)
mypy --strict src
make demo                      # scripted adversarial demo
```

## Definition of done (every PR)

- Tests first, from `tests/acceptance-scenarios.md` and the cited RFC section; full suite
  green including integration.
- All `spec/examples/*` still validate against their schemas.
- `mypy --strict` clean; public types/functions typed and docstring'd.
- The PR notes which RFC sections (or `CS-nnn` change-set items) it implements. Any
  unavoidable ambiguity is marked `# STONEFOLD-AMBIGUITY:` with the RFC reference.
- Specs (documents, schemas, fixtures) live only in the `spec/` submodule — if your change
  needs spec wording, a schema, or a fixture to move, land it in the
  [spec repo](https://github.com/stonefold-ai/spec) first, then bump the submodule pointer.

## Certifying your own gateway

You don't need to touch this repo's internals: implement one TCK driver (or the HTTP
harness for non-Python gateways) and run the kit — see `spec/docs/12-conformance-tck.md`. The
kit's core (`src/stonefold_tck/`) imports nothing from the reference implementation.

## License

Apache-2.0. By contributing you agree your contribution is licensed under the same terms
(Apache-2.0 §5).
