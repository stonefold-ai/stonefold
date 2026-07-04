# The Stonefold gateway service for the Accounts-Payable demo.
# Build context is the REPO ROOT (see docker-compose.yml) so the policy, registry,
# schema and source are all available to the image.
FROM python:3.11-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    # Survive an SSL-intercepting corporate proxy (harmless otherwise). Applies to
    # pip's build-isolation env too, which a plain --trusted-host flag would miss.
    PIP_TRUSTED_HOST="pypi.org files.pythonhosted.org pypi.python.org"

# Install deps first (cache-friendly): copy metadata + source the package needs.
COPY pyproject.toml ./
COPY src ./src
COPY examples ./examples
COPY registry ./registry
COPY schema ./schema
COPY demo ./demo

# Editable install so the gateway's Path(__file__).parents[2] still resolves to
# /app (where examples/, registry/, schema/ live).
RUN pip install -e ".[demo]"

EXPOSE 8088
CMD ["uvicorn", "stonefold_ap_demo.app:make_app", "--factory", "--host", "0.0.0.0", "--port", "8088"]
