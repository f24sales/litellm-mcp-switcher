# syntax=docker/dockerfile:1.7
# Verified wheels bootstrap trust updates without using an old pip CA bundle.
FROM scratch AS python-trust
ADD --checksum=sha256:fd7f4dfa8190e014013b9f08f3e973f4e4c26ffd179a204895533a7254813a70 \
    https://files.pythonhosted.org/packages/3f/06/3e4420302363fbb71be3c7f35ce042f54bfca330e3e66b3a2e3c4cca579f/pip-26.2.2-py3-none-any.whl /pip-26.2.2-py3-none-any.whl
ADD --checksum=sha256:42d35340cc5b9ba78e818ddbff0c5b1c2b779de448a340fd1b989f5fa8670e1b \
    https://files.pythonhosted.org/packages/c4/51/29046e48f693c54e5b7412216d447a1d8362c9653148d98c02cdf65fa733/certifi-2026.10.19-py3-none-any.whl /certifi-2026.10.19-py3-none-any.whl

FROM docker.io/library/alpine:3.24.3@sha256:b3413572228e34bd1c9245f5565e6cd127005655e8a7899aef33d8e83255c9e6 AS builder
RUN apk add --no-cache python3 py3-pip python3-dev build-base libffi-dev openssl-dev cargo rust
RUN --mount=type=bind,from=python-trust,target=/tmp/python-trust \
    python3 -c "import ensurepip, pathlib, shutil, sysconfig; seed = pathlib.Path(sysconfig.get_config_var('WHEEL_PKG_DIR') or pathlib.Path(ensurepip.__file__).with_name('_bundled')); [p.unlink() for p in seed.glob('pip-*.whl')]; shutil.copy('/tmp/python-trust/pip-26.2.2-py3-none-any.whl', seed); source = pathlib.Path(ensurepip.__file__); text = source.read_text(); old = ensurepip._PIP_VERSION; assert text.count(old) == 1; source.write_text(text.replace(old, '26.2.2')); ensurepip._PIP_VERSION = '26.2.2'; assert ensurepip.version() == '26.2.2'" \
    && PYTHONPATH=/tmp/python-trust/pip-26.2.2-py3-none-any.whl python3 -m pip install --break-system-packages --no-index --upgrade /tmp/python-trust/*.whl \
    && python3 -c "import ensurepip, pip, certifi, pip._vendor.certifi as bundled; assert ensurepip.version() == '26.2.2'; assert pip.__version__ == '26.2.2'; assert certifi.__version__ == '2026.10.19'; assert bundled.__version__ == '2026.10.19'"
WORKDIR /source
COPY pyproject.toml ./
COPY src ./src
RUN python3 -m pip wheel --wheel-dir=/wheels .

FROM docker.io/library/alpine:3.24.3@sha256:b3413572228e34bd1c9245f5565e6cd127005655e8a7899aef33d8e83255c9e6
RUN apk add --no-cache python3 py3-pip libpq ca-certificates libstdc++ bash openssl \
    && addgroup -g 10001 switcher && adduser -D -u 10001 -G switcher switcher \
    && mkdir -p /opt/f24sales/litellm-mcp-switcher/sqlite /etc/litellm-mcp-switcher \
    && chown -R switcher:switcher /opt/f24sales/litellm-mcp-switcher /etc/litellm-mcp-switcher
RUN --mount=type=bind,from=python-trust,target=/tmp/python-trust \
    python3 -c "import ensurepip, pathlib, shutil, sysconfig; seed = pathlib.Path(sysconfig.get_config_var('WHEEL_PKG_DIR') or pathlib.Path(ensurepip.__file__).with_name('_bundled')); [p.unlink() for p in seed.glob('pip-*.whl')]; shutil.copy('/tmp/python-trust/pip-26.2.2-py3-none-any.whl', seed); source = pathlib.Path(ensurepip.__file__); text = source.read_text(); old = ensurepip._PIP_VERSION; assert text.count(old) == 1; source.write_text(text.replace(old, '26.2.2')); ensurepip._PIP_VERSION = '26.2.2'; assert ensurepip.version() == '26.2.2'" \
    && PYTHONPATH=/tmp/python-trust/pip-26.2.2-py3-none-any.whl python3 -m pip install --break-system-packages --no-index --upgrade /tmp/python-trust/*.whl \
    && python3 -c "import ensurepip, pip, certifi, pip._vendor.certifi as bundled; assert ensurepip.version() == '26.2.2'; assert pip.__version__ == '26.2.2'; assert certifi.__version__ == '2026.10.19'; assert bundled.__version__ == '2026.10.19'"
COPY --from=builder /wheels /wheels
RUN python3 -m pip install --break-system-packages --no-index --find-links=/wheels litellm-mcp-switcher \
    && python3 -c "import ensurepip, pip, certifi, pip._vendor.certifi as bundled; assert ensurepip.version() == '26.2.2'; assert pip.__version__ == '26.2.2'; assert certifi.__version__ == '2026.10.19'; assert bundled.__version__ == '2026.10.19'" \
    && rm -rf /wheels /root/.cache
COPY litellm_add_virtual.py /usr/local/bin/litellm_add_virtual.py
COPY --chown=switcher:switcher config.sh env.example config.conf_example container.example optional_persistence.sh litellm_add_virtual.py /opt/f24sales/litellm-mcp-switcher/
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
USER switcher
WORKDIR /etc/litellm-mcp-switcher
EXPOSE 8080 8000
ENTRYPOINT ["litellm-mcp-switcher"]
