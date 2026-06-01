# syntax=docker/dockerfile:1.7
# -----------------------------------------------------------------------------
# Migration Validator container.
# Pure-Python SQL Server connector (python-tds) means no ODBC system deps.
# -----------------------------------------------------------------------------

ARG PYTHON_VERSION=3.12

FROM python:${PYTHON_VERSION}-slim-bookworm AS builder

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# libpq-dev is needed only for the psycopg build; the runtime image just needs libpq5.
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential libpq-dev \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/app
COPY pyproject.toml README.md /opt/app/
COPY src /opt/app/src
COPY mcp_servers /opt/app/mcp_servers

RUN pip install --upgrade pip \
 && pip install --target /opt/python-deps .

# -----------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim-bookworm AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/opt/python-deps:/opt/app

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates libpq5 tini \
 && rm -rf /var/lib/apt/lists/*

# Non-root user.
RUN groupadd --system --gid 10001 app \
 && useradd  --system --uid 10001 --gid app --create-home --shell /usr/sbin/nologin app

COPY --from=builder /opt/python-deps /opt/python-deps
COPY --from=builder /opt/app /opt/app

WORKDIR /opt/app
USER app

ENTRYPOINT ["/usr/bin/tini", "--", "python", "-m", "migration_validator"]
CMD ["--mode", "auto"]
