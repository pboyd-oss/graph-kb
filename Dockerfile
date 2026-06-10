ARG BASE_IMAGE=harbor.tuxgrid.com/docker.io/python:3.12-slim
FROM ${BASE_IMAGE}
WORKDIR /app

ARG PLATFORM_CA_B64
ARG HTTPS_PROXY
ARG HTTP_PROXY

# Install platform MITM CA so pip trusts the proxy during build
RUN if [ -n "$PLATFORM_CA_B64" ]; then \
      printf '%s' "$PLATFORM_CA_B64" | base64 -d \
        > /usr/local/share/ca-certificates/platform-ca.crt && \
      update-ca-certificates; \
    fi

# Route pip through the build proxy (mitmproxy sidecar)
ENV HTTPS_PROXY=$HTTPS_PROXY
ENV HTTP_PROXY=$HTTP_PROXY
ENV REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt

# CPU-only torch first — avoids pulling the 5GB CUDA build
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt


COPY server.py analyzer.py run_ingest.py ./
RUN mkdir -p /app/kb /app/documents

# Bake the embedding model into the image. HuggingFace is blocked by the build
# egress allowlist (only pypi/pytorch/etc are reachable via the mitmproxy), so the
# model is vendored into the repo and copied in instead of fetched at build time.
COPY model/all-MiniLM-L6-v2 /app/models/all-MiniLM-L6-v2
ENV EMBED_MODEL_PATH=/app/models/all-MiniLM-L6-v2
ENV SENTENCE_TRANSFORMERS_HOME=/app/models
ENV HF_HUB_OFFLINE=1
# Verify the vendored model loads fully offline (fails the build if a file is missing)
RUN python3 -c "import os; from sentence_transformers import SentenceTransformer; SentenceTransformer(os.environ['EMBED_MODEL_PATH'])"

# Clear proxy env so the runtime image doesn't use it
ENV HTTPS_PROXY=
ENV HTTP_PROXY=

CMD ["python", "server.py"]
