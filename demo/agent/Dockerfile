# The real-LLM agent runner for the Accounts-Payable demo.
# Same image contents as the gateway (it imports the same package); only the entry
# point differs — it is launched on demand via `docker compose run` (see Makefile).
FROM python:3.11-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_TRUSTED_HOST="pypi.org files.pythonhosted.org pypi.python.org"

COPY pyproject.toml ./
COPY src ./src
COPY examples ./examples
COPY registry ./registry
COPY schema ./schema
COPY demo ./demo

RUN pip install -e ".[demo]"

# Overridden by `make attack-safe` / `make attack-unsafe` / `make demo`.
CMD ["python", "-m", "acp_ap_demo.agent_cli", "--help"]
