"""
Graph KB — MCP server backed by LightRAG.

Drop documents or entire codebases into ./documents/ and query them via Claude.
Supports: .txt, .md, .pdf (documents) + .py, .groovy, Jenkinsfile, .js, .ts, .go, .rs, ... (code).
Backstage catalog ingestable via ingest_backstage tool.
"""
import asyncio
import hmac
import os
import threading
from pathlib import Path

import anthropic
import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import PlainTextResponse
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

_KB_TOKEN = os.getenv("GRAPH_KB_TOKEN", "")


class _BearerAuth(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        if not _KB_TOKEN:
            return await call_next(request)
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return PlainTextResponse("Unauthorized", status_code=401)
        if not hmac.compare_digest(auth.removeprefix("Bearer "), _KB_TOKEN):
            return PlainTextResponse("Unauthorized", status_code=401)
        return await call_next(request)

from analyzer import (
    CODE_EXTENSIONS, SPECIAL_FILENAMES,
    analyze_directory, analyze_file, is_code_file,
    build_terraform_cross_repo_graph,
)

DOCS_DIR = Path("/app/documents")
KB_DIR = "/app/kb"

DOC_EXTENSIONS = {".txt", ".md", ".pdf"}
ALL_SUPPORTED = DOC_EXTENSIONS | set(CODE_EXTENSIONS)


# ── Embeddings (local sentence-transformers, no extra API key) ───────────────

_embed_model = None
_embed_model_lock = threading.Lock()


def _get_embed_model():
    global _embed_model
    if _embed_model is None:
        with _embed_model_lock:
            if _embed_model is None:
                from sentence_transformers import SentenceTransformer
                _embed_model = SentenceTransformer("all-MiniLM-L6-v2")
    return _embed_model


async def _embed(texts: list[str]):
    return _get_embed_model().encode(texts, normalize_embeddings=True)


# ── LLM (Claude via Anthropic SDK) ──────────────────────────────────────────

_anthropic = anthropic.AsyncAnthropic()


async def _llm(prompt, system_prompt=None, history_messages=None, **kwargs) -> str:
    history_messages = history_messages or []
    messages = [{"role": h["role"], "content": h["content"]} for h in history_messages]
    messages.append({"role": "user", "content": prompt})
    resp = await _anthropic.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4096,
        system=system_prompt or "You are a helpful knowledge extraction assistant.",
        messages=messages,
    )
    return resp.content[0].text


# ── LightRAG ─────────────────────────────────────────────────────────────────

_rag = None
_rag_lock = threading.Lock()


def _get_rag():
    global _rag
    if _rag is None:
        with _rag_lock:
            if _rag is None:
                from lightrag import LightRAG
                from lightrag.utils import EmbeddingFunc
                embedding_func = EmbeddingFunc(embedding_dim=384, max_token_size=8192, func=_embed)
                rag = LightRAG(working_dir=KB_DIR, llm_model_func=_llm, embedding_func=embedding_func)
                asyncio.run_coroutine_threadsafe(rag.initialize_storages(), _rag_loop).result()
                _rag = rag
    return _rag


# Dedicated event loop for LightRAG — all rag.a* calls go here, keeping the
# graph operations single-threaded and avoiding asyncio lock cross-loop issues.
_rag_loop = asyncio.new_event_loop()
threading.Thread(target=_rag_loop.run_forever, daemon=True, name="rag-loop").start()


def _submit(coro) -> asyncio.Future:
    """Submit a coroutine to the RAG loop and return a concurrent Future."""
    return asyncio.run_coroutine_threadsafe(coro, _rag_loop)


async def _await(coro):
    """Await a coroutine that runs in the RAG loop (from MCP tool handlers)."""
    return await asyncio.wrap_future(_submit(coro))


# ── File readers ─────────────────────────────────────────────────────────────

def _read_doc(path: Path) -> str:
    if path.suffix == ".pdf":
        from pypdf import PdfReader
        return "\n".join(page.extract_text() or "" for page in PdfReader(str(path)).pages)
    return path.read_text(encoding="utf-8", errors="ignore")


def _read_code(path: Path) -> str:
    source = path.read_text(encoding="utf-8", errors="ignore")
    return analyze_file(path, source)


def _read_file(path: Path) -> str:
    if is_code_file(path):
        return _read_code(path)
    return _read_doc(path)


# ── Watchdog ─────────────────────────────────────────────────────────────────

def _ingest_text(text: str, label: str = ""):
    future = _submit(_get_rag().ainsert(text))
    future.add_done_callback(
        lambda f: print(
            f"[OK] {label}" if not f.exception() else f"[ERR] {label}: {f.exception()}",
            flush=True,
        )
    )


class DocHandler(FileSystemEventHandler):
    def on_created(self, event):
        p = Path(event.src_path)

        if event.is_directory:
            # A codebase folder was dropped in — analyze the whole tree
            print(f"[DIR] Analyzing codebase: {p.name}", flush=True)
            try:
                for file_path, analysis in analyze_directory(p):
                    _ingest_text(analysis, label=str(file_path.relative_to(DOCS_DIR)))
            except Exception as e:
                print(f"[ERR] Codebase analysis failed: {e}", flush=True)
            return

        if p.suffix not in ALL_SUPPORTED and p.name not in SPECIAL_FILENAMES:
            return

        print(f"[FILE] Ingesting: {p.name}", flush=True)
        try:
            _ingest_text(_read_file(p), label=p.name)
        except Exception as e:
            print(f"[ERR] {p.name}: {e}", flush=True)


# ── Backstage catalog helpers ────────────────────────────────────────────────

async def _fetch_backstage_entities(base_url: str, token: str | None, kinds: list[str]) -> list[dict]:
    import httpx
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    entities: list[dict] = []

    async with httpx.AsyncClient(headers=headers, timeout=30, follow_redirects=True) as client:
        for kind in kinds:
            cursor: str | None = None
            while True:
                params: dict = {"filter": f"kind={kind}", "limit": "200"}
                if cursor:
                    params["cursor"] = cursor
                resp = await client.get(f"{base_url}/api/catalog/entities", params=params)
                resp.raise_for_status()
                data = resp.json()
                # Backstage returns either a plain list or a paginated {items, pageInfo} object
                if isinstance(data, list):
                    entities.extend(data)
                    break
                entities.extend(data.get("items", []))
                cursor = data.get("pageInfo", {}).get("nextCursor")
                if not cursor:
                    break

    return entities


def _fmt_entity(entity: dict) -> str:
    kind = entity.get("kind", "Unknown")
    meta = entity.get("metadata", {})
    spec = entity.get("spec", {})
    name = meta.get("name", "unknown")
    desc = meta.get("description", "")
    tags = meta.get("tags", [])

    out = [f"Backstage {kind}: {name}", ""]
    if desc:
        out += [f"Description: {desc}", ""]

    for field in ["type", "lifecycle", "owner", "system", "domain"]:
        if field in spec:
            out.append(f"{field.capitalize()}: {spec[field]}")
    if tags:
        out.append(f"Tags: {', '.join(tags)}")
    out.append("")

    rel_map = [
        ("dependsOn",      "Depends On"),
        ("providesApis",   "Provides APIs"),
        ("consumesApis",   "Consumes APIs"),
        ("subcomponentOf", "Subcomponent Of"),
        ("memberOf",       "Member Of"),
        ("members",        "Members"),
    ]
    for key, label in rel_map:
        val = spec.get(key)
        if not val:
            continue
        if isinstance(val, list):
            out.append(f"{label}:")
            out.extend(f"  - {v}" for v in val[:30])
        else:
            out.append(f"{label}: {val}")
        out.append("")

    # Surface useful annotations
    annotations = meta.get("annotations", {})
    for ann_key, label in [
        ("github.com/project-slug",    "GitHub repo"),
        ("gitlab.com/project-slug",    "GitLab repo"),
        ("jenkins.io/job-full-name",   "Jenkins job"),
        ("sonarqube.org/project-key",  "SonarQube project"),
        ("pagerduty.com/service-id",   "PagerDuty service"),
        ("backstage.io/techdocs-ref",  "TechDocs"),
        ("runbook-url",                "Runbook"),
        ("grafana/dashboard-selector", "Grafana dashboard"),
    ]:
        if ann_key in annotations:
            out.append(f"{label}: {annotations[ann_key]}")

    return "\n".join(out)


def _fmt_system_map(entities: list[dict]) -> str:
    by_system: dict[str, list[str]] = {}
    deps: list[str] = []

    for e in entities:
        kind = e.get("kind", "")
        meta = e.get("metadata", {})
        spec = e.get("spec", {})
        name = meta.get("name", "")
        system = spec.get("system", "")
        if system:
            by_system.setdefault(system, []).append(f"{kind.lower()}:{name}")
        for dep in spec.get("dependsOn", []):
            deps.append(f"{name} depends on {dep}")

    out = ["Backstage System Map", "=" * 30, ""]
    if by_system:
        out.append("Systems and their components:")
        for system, components in sorted(by_system.items()):
            out.append(f"\n  {system}:")
            out.extend(f"    - {c}" for c in sorted(components))
        out.append("")
    if deps:
        out.append("Dependency relationships:")
        out.extend(f"  - {d}" for d in deps[:200])
        out.append("")
    out += [
        "This system map is in the knowledge graph.",
        "Query: who owns X, what does Y depend on, what APIs does Z provide.",
    ]
    return "\n".join(out)


# ── MCP server ────────────────────────────────────────────────────────────────

mcp = FastMCP(
    "graph-kb",
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


@mcp.tool()
async def ingest(text: str, label: str = "document") -> str:
    """Add raw text to the knowledge graph."""
    await _await(_get_rag().ainsert(text))
    return f"Ingested '{label}'."


@mcp.tool()
async def ingest_file(filename: str) -> str:
    """Ingest a file from the documents folder by name."""
    p = DOCS_DIR / filename
    if not p.exists():
        return f"File not found: {filename}"
    if p.suffix not in ALL_SUPPORTED and p.name not in SPECIAL_FILENAMES:
        return f"Unsupported type '{p.suffix}'. Supported: {', '.join(sorted(ALL_SUPPORTED))}"
    await _await(_get_rag().ainsert(_read_file(p)))
    return f"Ingested {filename}."


@mcp.tool()
async def analyze_codebase(directory: str) -> str:
    """
    Analyze a code directory and ingest function/class/import relationships.
    Pass a folder name relative to the documents directory (e.g. 'my-project').
    """
    dir_path = DOCS_DIR / directory
    if not dir_path.is_dir():
        return f"Directory not found: {directory}"

    results = analyze_directory(dir_path)
    if not results:
        return f"No supported code files found in '{directory}'."

    for file_path, analysis in results:
        await _await(_get_rag().ainsert(analysis))

    langs: dict[str, int] = {}
    for p, _ in results:
        ext = p.suffix
        langs[ext] = langs.get(ext, 0) + 1

    summary = ", ".join(f"{ext} ({n})" for ext, n in sorted(langs.items()))
    return f"Analyzed {len(results)} files from '{directory}': {summary}"


@mcp.tool()
async def analyze_terraform_repos(directories: list[str] | None = None) -> str:
    """
    Build a cross-repo Terraform dependency graph and ingest it.

    Pass a list of folder names under documents/ (e.g. ['infra-vpc', 'infra-eks']).
    If omitted, auto-discovers all subdirectories that contain .tf files.

    Surfaces: module source chains, remote state reads, output→variable wiring,
    and a full resource inventory across repos.
    """
    if directories:
        dirs = [DOCS_DIR / d for d in directories]
        missing = [str(d) for d in dirs if not d.is_dir()]
        if missing:
            return f"Not found: {', '.join(missing)}"
    else:
        dirs = [
            d for d in DOCS_DIR.iterdir()
            if d.is_dir() and any(d.rglob("*.tf"))
        ]
        if not dirs:
            return "No Terraform repos found in documents/. Drop your repo folders there first."

    graph = build_terraform_cross_repo_graph(dirs)
    await _await(_get_rag().ainsert(graph))
    return f"Cross-repo graph built for {len(dirs)} repos: {', '.join(d.name for d in dirs)}"


@mcp.tool()
async def ingest_backstage(
    token: str | None = None,
    kinds: list[str] | None = None,
) -> str:
    """
    Ingest the Backstage software catalog into the knowledge graph.

    Pulls all catalog entities and their relationships (dependsOn, providesApis,
    consumesApis, ownedBy, partOf) plus a cross-entity system map.

    The Backstage URL is read from the BACKSTAGE_URL environment variable only.
    token:    Bearer token. Falls back to BACKSTAGE_TOKEN env var.
              Omit if your Backstage allows unauthenticated reads.
    kinds:    Entity kinds to fetch. Defaults to Component, API, Resource,
              System, Group, Domain.
    """
    url = os.getenv("BACKSTAGE_URL", "").rstrip("/")
    tok = token or os.getenv("BACKSTAGE_TOKEN") or None

    if not url:
        return "No Backstage URL. Pass base_url or set BACKSTAGE_URL in .env."

    entity_kinds = kinds or ["Component", "API", "Resource", "System", "Group", "Domain"]

    try:
        entities = await _fetch_backstage_entities(url, tok, entity_kinds)
    except Exception as exc:
        return f"Failed to fetch Backstage catalog: {exc}"

    if not entities:
        return "No entities returned — check the URL and token."

    for entity in entities:
        await _await(_get_rag().ainsert(_fmt_entity(entity)))

    await _await(_get_rag().ainsert(_fmt_system_map(entities)))

    counts: dict[str, int] = {}
    for e in entities:
        counts[e.get("kind", "?")] = counts.get(e.get("kind", "?"), 0) + 1
    summary = ", ".join(f"{k}: {n}" for k, n in sorted(counts.items()))
    return f"Ingested {len(entities)} Backstage entities — {summary}"


@mcp.tool()
async def query(question: str, mode: str = "hybrid") -> str:
    """
    Query the knowledge graph.
    mode: hybrid (default) | local (precise entity lookup) | global (big-picture) | naive (simple vector search)
    """
    from lightrag import QueryParam
    return await _await(_get_rag().aquery(question, param=QueryParam(mode=mode)))


@mcp.tool()
async def list_documents() -> list[str]:
    """List files and folders currently in the documents directory."""
    entries = []
    for p in sorted(DOCS_DIR.iterdir()):
        if p.is_dir():
            count = sum(1 for _ in p.rglob("*") if _.is_file())
            entries.append(f"{p.name}/ ({count} files)")
        else:
            entries.append(p.name)
    return entries


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    observer = Observer()
    observer.schedule(DocHandler(), str(DOCS_DIR), recursive=False)
    observer.start()
    print("Graph KB started — watching /app/documents", flush=True)
    try:
        app = mcp.sse_app()
        app.add_middleware(_BearerAuth)
        uvicorn.run(app, host="0.0.0.0", port=8000)
    finally:
        observer.stop()
        observer.join()
