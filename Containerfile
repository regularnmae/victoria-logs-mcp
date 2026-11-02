# uv is the only build tool end to end: it turns uv.lock into a
# hash-pinned requirements file, and it installs that file. No pip
# anywhere. The uv binary is copied in from its image, used, and deleted,
# so nothing but Python and the app remain in the image.

# The base is pinned by tag *and* digest: the tag keeps the pin readable and
# gives Renovate a stream to follow, the digest makes a moved tag a no-op
# until a commit bumps it.
FROM docker.io/library/python:3.14-alpine@sha256:7f2ec713f0436c59df2362d973b6a05af9e87ba78f0e94f5245c302984a7a1fd

# Unbuffered stdout, so `podman logs` shows lines as they happen.
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Just the uv binary, taken straight out of its image (same tag+digest
# reasoning as the base) rather than a whole second build stage for it.
COPY --from=ghcr.io/astral-sh/uv:0.13.0@sha256:20d3fe1308ffa40b51c5c20e002bab5fece40585edcee8ec99357309e8363424 \
     /usr/local/bin/uv /usr/local/bin/uv

# The lock, not a bare dependency list: stack/deploy.sh rebuilds from scratch
# on every deploy, and an unpinned closure means a rebuild can silently
# change the transitive tree. The hashes come from uv export for free, and
# --require-hashes means uv actually enforces them. Both build tools leave
# the image after they are done with it; the lockfiles are inputs to this
# step only, so they go too.
COPY pyproject.toml uv.lock ./
RUN uv export --format requirements-txt --no-dev --no-emit-project --locked -q \
      -o /tmp/requirements.txt \
    && uv pip install --system --no-cache --require-hashes -r /tmp/requirements.txt \
    && rm -f /usr/local/bin/uv /tmp/requirements.txt pyproject.toml uv.lock \
    && python -m pip uninstall -y pip

COPY server.py .
COPY victorialogs.py .
COPY healthcheck.py .

USER nobody
EXPOSE 8000
CMD ["python", "server.py"]
