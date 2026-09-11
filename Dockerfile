# The studio: the brain, the painter and the API, in one container.
#
# The 540 MB connectome release is not downloaded at build time. The built graph
# (build/graph.npz, 38 MB, deterministic — its sha256 is stamped into every piece)
# is fetched from the GitHub release instead, so the image builds in a minute
# on any host. To rebuild the graph from the release yourself:
#   python run.py fetch && python run.py build
#
#   docker build -t connectome-canvas .
#   docker run -p 4660:4660 -e CANVAS_TICKS=600 connectome-canvas

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    CANVAS_PORT=4660 \
    CANVAS_TICKS=600 \
    CANVAS_TICK_MS=10 \
    CANVAS_PAUSE_S=30

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt

ARG GRAPH_URL=https://github.com/bnbhacker/connectome-canvas/releases/download/graph-v1.0/graph.npz
RUN mkdir -p build && curl -fL --retry 5 -o build/graph.npz "$GRAPH_URL"

COPY brain ./brain
COPY canvas ./canvas
COPY chain ./chain
COPY site ./site
COPY server.py run.py ./
RUN mkdir -p gallery site/recordings

EXPOSE 4660
CMD ["sh", "-c", "python run.py serve --host 0.0.0.0 --port ${PORT:-4660}"]
