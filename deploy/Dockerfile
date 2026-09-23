# syntax=docker/dockerfile:1
# PR Flagger, the always-on service. Build from the repository root:
#     docker build -f deploy/Dockerfile -t prflagger .
# It runs pull requests in sibling containers on the host's Docker, so it needs the
# host's Docker socket and a data directory mounted at the same path inside and out
# — deploy/compose.yaml sets both up. See DEPLOY.md.

# The slim base keeps the image small; any Python 3.11+ image works. One that
# already carries git and curl (e.g. python:3.11) needs no package archive at all.
ARG BASE_IMAGE=python:3.11-slim
FROM ${BASE_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

# Building behind a restrictive or TLS-intercepting proxy (common on corporate
# networks) needs two optional inputs, neither of which ends up in the image:
#   --build-arg APT_MIRROR=https://deb.debian.org   when plain-HTTP apt is blocked
#   --secret id=proxy_ca,src=/path/to/proxy-ca.crt   the proxy's CA, as a build secret
# plus the usual --build-arg https_proxy=... . On an ordinary network, pass nothing.
ARG APT_MIRROR=""

# git clones repositories; curl is for the health check. The Docker CLI (only the
# client — the daemon is the host's) comes from Docker's own image.
RUN --mount=type=secret,id=proxy_ca,required=false \
    if [ -f /run/secrets/proxy_ca ]; then \
      echo 'Acquire::https::CAInfo "/run/secrets/proxy_ca";' > /etc/apt/apt.conf.d/99proxy-ca; \
    fi \
 && if [ -n "$APT_MIRROR" ]; then \
      sed -i "s|http://deb.debian.org|$APT_MIRROR|g" /etc/apt/sources.list.d/debian.sources; \
    fi \
 && if ! command -v git >/dev/null || ! command -v curl >/dev/null; then \
      apt-get update \
      && apt-get install -y --no-install-recommends git curl ca-certificates \
      && rm -rf /var/lib/apt/lists/*; \
    fi \
 && rm -f /etc/apt/apt.conf.d/99proxy-ca
COPY --from=docker:cli /usr/local/bin/docker /usr/local/bin/docker

WORKDIR /src
COPY pyproject.toml README.md ./
COPY prflagger ./prflagger
RUN --mount=type=secret,id=proxy_ca,required=false \
    if [ -f /run/secrets/proxy_ca ]; then export PIP_CERT=/run/secrets/proxy_ca; fi \
 && pip install . && rm -rf /src

# Not root. Access to the Docker socket is granted by adding the host's docker
# group at run time (compose: group_add), because its id differs between hosts.
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin prflagger
USER prflagger
WORKDIR /var/lib/prflagger

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8000/api/health >/dev/null || exit 1

ENTRYPOINT ["prflagger"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8000"]
