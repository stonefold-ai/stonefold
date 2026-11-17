# Stonefold Python prototype

Research-only gateway prototype and conformance test kit. This README preserves build instructions for the existing code. Earlier architecture prose has been retired; this prototype does not define requirements for future applications. No production safety guarantee is claimed.

## Development

Create and activate a Python virtual environment, then run from this directory:

```sh
pip install -e ".[dev,gateway,demo]"
pytest -m "not integration"
```

Docker-backed tests use `pytest -m integration`; inspect test configuration for infrastructure requirements. Schemas and fixtures remain in `spec/`.

## Scripted demonstration

```sh
pip install -e ".[demo]"
python -m stonefold_ap_demo --provider fake
```

This uses a scripted actor and fictional in-memory bank. It is not a real-model evaluation or production payment-safety demonstration. Source and tests remain the reference for the preserved implementation's behavior.

See [security reporting](SECURITY.md) and [contribution instructions](CONTRIBUTING.md).
