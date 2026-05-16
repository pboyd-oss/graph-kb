FROM harbor.tuxgrid.com/docker.io/python:3.12-slim
WORKDIR /app

ARG PLATFORM_CA_B64
ARG HTTPS_PROXY=http://localhost:8080
ARG HTTP_PROXY=http://localhost:8080
ENV HTTPS_PROXY=$HTTPS_PROXY
ENV HTTP_PROXY=$HTTP_PROXY

RUN if [ -n "$PLATFORM_CA_B64" ]; then \
      echo "$PLATFORM_CA_B64" | base64 -d > /usr/local/share/ca-certificates/platform-ca.crt && \
      update-ca-certificates; \
    fi

# CPU-only torch first — avoids pulling the 5GB CUDA build
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py analyzer.py ./
RUN mkdir -p /app/kb /app/documents

CMD ["python", "server.py"]
