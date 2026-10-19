# Build context: repository root.
FROM python:3.15-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends bash git curl ca-certificates procps \
 && rm -rf /var/lib/apt/lists/* \
 && git config --system user.name syagent \
 && git config --system user.email syagent@localhost \
 && git config --system init.defaultBranch main

COPY seed/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --root-user-action=ignore -r /tmp/requirements.txt && rm /tmp/requirements.txt

COPY seed/ /opt/syagent/seed/
COPY sandbox/agent-entrypoint.sh /opt/syagent/entrypoint.sh

WORKDIR /agent
EXPOSE 7000
ENTRYPOINT ["/opt/syagent/entrypoint.sh"]
