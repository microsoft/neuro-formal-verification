# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

FROM python:3.12-bookworm

RUN test "$(dpkg --print-architecture)" = amd64 \
    && apt-get update -qq \
    && apt-get install -y --no-install-recommends ca-certificates curl git gh unzip \
       libicu72 libssl3 libgssapi-krb5-2 \
    && rm -rf /var/lib/apt/lists/*

RUN curl --fail --location --silent --show-error \
      https://github.com/dafny-lang/dafny/releases/download/v4.11.0/dafny-4.11.0-x64-ubuntu-22.04.zip \
      --output /tmp/dafny.zip \
    && echo "a46a9ff7cdd720f7955854c78e95df13f4cfe6b80691b05f8654fe19e8267179  /tmp/dafny.zip" \
       | sha256sum --check \
    && unzip -q /tmp/dafny.zip -d /opt/nfv-verifier \
    && rm /tmp/dafny.zip \
    && ln -s "$(find /opt/nfv-verifier/dafny -type f -name z3-4.12.1 -print -quit)" /usr/local/bin/nfv-z3

ENV NFV_DAFNY_PATH=/opt/nfv-verifier/dafny/dafny \
    NFV_Z3_PATH=/usr/local/bin/nfv-z3 \
    PYTHONUNBUFFERED=1

WORKDIR /opt/nfv
COPY . .
RUN python -m pip install --no-cache-dir ".[inference]" "./workflow[local]" \
    && python -c "from copilot import CopilotClient; import subprocess; subprocess.run([CopilotClient()._connection.path, '--version'], check=True)" \
    && python -m nfv.shared.config \
    && test ! -e .git && test ! -e paper/data && test ! -e paper/results \
    && test ! -e workflow/tests && test ! -e .env \
    && nfv-workflow local --help

WORKDIR /run/nfv
ENTRYPOINT ["nfv-workflow"]
CMD ["local", "--help"]
