FROM docker.io/library/alpine:3.24.3@sha256:b3413572228e34bd1c9245f5565e6cd127005655e8a7899aef33d8e83255c9e6 AS builder
RUN apk add --no-cache python3 py3-pip python3-dev build-base libffi-dev openssl-dev cargo rust
WORKDIR /source
COPY pyproject.toml ./
COPY src ./src
RUN python3 -m pip wheel --wheel-dir=/wheels .

FROM docker.io/library/alpine:3.24.3@sha256:b3413572228e34bd1c9245f5565e6cd127005655e8a7899aef33d8e83255c9e6
RUN apk add --no-cache python3 py3-pip libpq ca-certificates libstdc++ \
    && addgroup -g 10001 switcher && adduser -D -u 10001 -G switcher switcher \
    && mkdir -p /opt/f24sales/litellm-mcp-switcher/sqlite /etc/litellm-mcp-switcher \
    && chown -R switcher:switcher /opt/f24sales/litellm-mcp-switcher /etc/litellm-mcp-switcher
COPY --from=builder /wheels /wheels
RUN python3 -m pip install --break-system-packages --no-index --find-links=/wheels litellm-mcp-switcher \
    && rm -rf /wheels /root/.cache
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
USER switcher
WORKDIR /etc/litellm-mcp-switcher
EXPOSE 8080 8000
ENTRYPOINT ["litellm-mcp-switcher"]
