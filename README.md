# Stonefold

Research into when external controls make AI-driven business operations safer, what assumptions those controls depend on, and what operational costs they introduce.

This repository contains a Python gateway prototype, a conformance test kit, and runnable demonstrations. The implementation is an experimental instrument. The research should establish when existing application controls are sufficient as well as when an additional enforcement layer helps.

## Status

Working prototype, not production-hardened. The project is moving from general gateway development towards focused comparative research. The documentation and architecture are under review; older design and positioning documents describe the previous direction. No completed independent comparison or production safety guarantee is claimed.

The current implementation evaluates declared operations against policy, supports held actions and approvals, stages effects, and records decisions. Its guarantees depend on the actual connectors, deployment boundaries, trusted state, and enabled checks. A successful policy decision is not by itself proof of a correct business outcome.

## Start with the demonstration

Clone with the specification submodule populated:

```bash
git clone --recurse-submodules https://github.com/stonefold-ai/stonefold.git
cd stonefold
python -m venv .venv
```

Activate the environment (`.venv\Scripts\Activate.ps1` in PowerShell, or `source .venv/bin/activate` in a POSIX shell), then:

```bash
pip install -e ".[demo]"
python -m stonefold_ap_demo --provider fake
```

This walkthrough uses a scripted agent and an in-memory bank with fictional data. It demonstrates allowed, held and refused payments, approval release, and audit records. It does not evaluate a real model or demonstrate production payment safety. See the [demo runbook](demo/README.md) for the UI and optional infrastructure/model setup.

## Research direction

- Whether an approval remains bound to the action actually executed.
- What bypasses, out-of-band changes, and untrusted state invalidate a control.
- How concurrency, retries, stale decisions, and ambiguous outcomes affect enforcement.
- How much valid work a control blocks or delays, compared with competent existing controls.

These are proposed directions, not completed comparative findings. Each study needs a precise question, related-work review, reproducible configuration, an application baseline, and observations of downstream effects.

## Read further

| Need | Start here |
|---|---|
| Run the current example | [Demo](demo/README.md) |
| Explore implementation examples | [Guide](guide/README.md) |
| Find technical and historical documents | [Documentation index](docs/README.md) |
| Inspect current protocol and policy definitions | [Specification](https://github.com/stonefold-ai/spec) |
| Understand conformance scope | [TCK specification](spec/docs/12-conformance-tck.md) |

The specification remains the current implementation contract while architecture changes are reviewed. Its existing terminology is not a prerequisite for understanding the research purpose. Conformance to our own specification is not independent validation of real-world safety.

## Development

```bash
pip install -e ".[dev,gateway,demo]"
pytest -m "not integration"
```

Integration tests require Docker-backed infrastructure; see test markers and individual runbooks before running them. The gateway's stores, connectors, and identity provider must be assessed in the configuration used for each experiment.

## Limitations

A gateway cannot establish that a business policy is correct or control paths outside its enforcement boundary. Synthetic scenarios do not establish how common failures are in real companies. The demonstration is not a comparative benchmark, and a model avoiding one attack does not establish a durable restriction on its authority.

## License

[Apache-2.0](LICENSE). Provided as is, without warranty.
