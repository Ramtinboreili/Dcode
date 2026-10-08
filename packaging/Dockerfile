# Dcode in a container: the self-contained binary plus git (Dcode shells out to it).
#
#   make docker                      # builds dcode:<version> and dist/dcode-<version>-docker-<arch>.tar.gz
#   docker load -i dcode-…-docker-….tar.gz        # on the air-gapped host
#   docker run --rm -it --network host -v "$PWD:/workspace" -v dcode-home:/home/dcode/.dcode \
#       -e DCODE_BASE_URL=http://127.0.0.1:11434/v1 -e DCODE_API_KEY=local -e DCODE_MODEL=qwen2.5-coder:7b dcode:<version>
#
# --network host lets Dcode reach a model server on the host and lets your browser reach the
# web UI (it listens on 127.0.0.1 only). Without it, add --no-ui and point DCODE_BASE_URL at a
# reachable address. The volume keeps chats, memory and settings between runs.
FROM debian:12-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 dcode \
    && mkdir -p /workspace /home/dcode/.dcode \
    && chown dcode:dcode /workspace /home/dcode/.dcode \
    && git config --system --add safe.directory '*'
COPY dist/dcode /usr/local/bin/dcode
USER dcode
ENV TERM=xterm-256color
WORKDIR /workspace
ENTRYPOINT ["dcode"]
