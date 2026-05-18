# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Local Build & Deploy

Use this for fast iteration instead of the Jenkins pipeline.

### Prerequisites

- `docker`, `crane`, `cosign` installed locally
- Logged in to Harbor: `echo <token> | crane auth login harbor.tuxgrid.com -u "robot\$platform+jenkins" --password-stdin`
- `kubectl` context pointing at the cluster
- Cosign private key: `kubectl get secret cosign-key -n jenkins -o jsonpath='{.data.cosign\.key}' | base64 -d > /tmp/cosign.key && chmod 600 /tmp/cosign.key`

### Build

```bash
docker buildx build \
  --platform linux/amd64 \
  --push \
  -t harbor.tuxgrid.com/platform/graph-kb:latest \
  .
```

The model (`all-MiniLM-L6-v2`) is baked into the image at `/app/models`. First build is slow (downloads torch CPU wheel + model); subsequent builds use the Docker layer cache.

### Sign

```bash
DIGEST=$(crane digest harbor.tuxgrid.com/platform/graph-kb:latest)
COSIGN_PASSWORD="" SIGSTORE_REKOR_API_URL="" cosign sign \
  --key /tmp/cosign.key \
  --yes \
  "harbor.tuxgrid.com/platform/graph-kb@${DIGEST}"

rm -f /tmp/cosign.key
```

Signing is required — Kyverno's `require-signed-platform-images` policy blocks pod creation without it.

### Deploy

```bash
DIGEST=$(crane digest harbor.tuxgrid.com/platform/graph-kb:latest)
kubectl set image deployment/graph-kb \
  graph-kb="harbor.tuxgrid.com/platform/graph-kb@${DIGEST}" \
  -n graph-kb
kubectl rollout status deployment/graph-kb -n graph-kb
```

## Architecture

- **`server.py`**: FastMCP SSE server on `:8000`. The `SentenceTransformer` model and `LightRAG` are lazy-initialized on first tool call, so the server binds the port immediately at startup. All LightRAG operations run in a dedicated asyncio event loop on a separate thread (`rag-loop`) to keep graph operations single-threaded.
- **`analyzer.py`**: Code analysis for Python, Go, Groovy, Terraform, etc. Produces structured summaries for ingestion.
- **Model**: `all-MiniLM-L6-v2` (384-dim) baked into image at `/app/models`; `HF_HUB_OFFLINE=1` prevents network calls at runtime.
- **Storage**: Two PVCs — `graph-kb-data` (`/app/kb`, the LightRAG graph/index) and `graph-kb-documents` (`/app/documents`, watched by watchdog for auto-ingestion).
- **Auth**: Bearer token via `GRAPH_KB_TOKEN` secret; readiness probe uses `tcpSocket` (not httpGet) since `/sse` requires auth.

## Secrets

All in `graph-kb-secrets` SealedSecret (`k8s/sealedsecret.yaml`). To reseal after updating a secret value:

```bash
kubectl get secret graph-kb-secrets -n graph-kb -o json \
  | python3 -c "import sys,json; d=json.load(sys.stdin); d['metadata']={k:v for k,v in d['metadata'].items() if k in ('name','namespace')}; d.pop('status',None); print(json.dumps(d))" \
  | kubeseal --controller-namespace sealed-secrets --controller-name sealed-secrets --format yaml \
  > k8s/sealedsecret.yaml
```
