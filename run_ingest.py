"""
Ingest all documents and code directories from /app/documents into the knowledge graph.

Runs as a standalone process (Kubernetes Job). Connects to the graph-kb server's
MCP SSE endpoint and calls analyze_codebase / ingest_file for each item in /app/documents.

Usage:
    python3 run_ingest.py [--server http://graph-kb.graph-kb.svc.cluster.local:8000]
"""
import asyncio
import os
import argparse

import httpx

SERVER = os.getenv("KB_URL", "http://graph-kb.graph-kb.svc.cluster.local:8000")
TOKEN = os.getenv("GRAPH_KB_TOKEN", "")


def _headers():
    h = {"Accept": "text/event-stream"}
    if TOKEN:
        h["Authorization"] = f"Bearer {TOKEN}"
    return h


async def _call_tool(client: httpx.AsyncClient, session_id: str, name: str, arguments: dict) -> str:
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }
    resp = await client.post(
        f"{SERVER}/messages/?session_id={session_id}",
        json=payload,
        headers={**({"Authorization": f"Bearer {TOKEN}"} if TOKEN else {}), "Content-Type": "application/json"},
        timeout=7200,  # tools can run for hours
    )
    resp.raise_for_status()
    return resp.text


async def _open_session(client: httpx.AsyncClient) -> tuple[str, asyncio.Task]:
    """Open an SSE connection and return (session_id, background_task)."""
    session_id = None
    ready = asyncio.Event()

    async def _drain():
        nonlocal session_id
        async with client.stream("GET", f"{SERVER}/sse", headers=_headers(), timeout=None) as r:
            r.raise_for_status()
            async for line in r.aiter_lines():
                if line.startswith("data:"):
                    data = line[5:].strip()
                    if "session_id=" in data:
                        session_id = data.split("session_id=")[1].split('"')[0].split("&")[0]
                        ready.set()
                # keep reading to hold the session open

    task = asyncio.create_task(_drain())
    await asyncio.wait_for(ready.wait(), timeout=30)
    return session_id, task


async def _list_documents(client: httpx.AsyncClient) -> tuple[list[str], list[str]]:
    resp = await client.get(
        f"{SERVER}/documents",
        headers={**({"Authorization": f"Bearer {TOKEN}"} if TOKEN else {})},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    return data["directories"], data["files"]


async def main(server: str):
    global SERVER
    SERVER = server.rstrip("/")

    async with httpx.AsyncClient() as client:
        print("Fetching document list from server...", flush=True)
        dirs, files = await _list_documents(client)

    if not dirs and not files:
        print("No items in /app/documents — nothing to ingest.", flush=True)
        return

    print(f"Found {len(dirs)} directories and {len(files)} loose files to ingest.", flush=True)

    async with httpx.AsyncClient() as client:
        # Initialize MCP session
        print("Opening MCP session...", flush=True)
        session_id, sse_task = await _open_session(client)
        print(f"Session: {session_id}", flush=True)

        # Initialize the MCP connection
        await client.post(
            f"{SERVER}/messages/?session_id={session_id}",
            json={"jsonrpc": "2.0", "id": 0, "method": "initialize",
                  "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                             "clientInfo": {"name": "ingest-job", "version": "1.0"}}},
            headers={**({"Authorization": f"Bearer {TOKEN}"} if TOKEN else {}), "Content-Type": "application/json"},
            timeout=30,
        )
        await client.post(
            f"{SERVER}/messages/?session_id={session_id}",
            json={"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
            headers={**({"Authorization": f"Bearer {TOKEN}"} if TOKEN else {}), "Content-Type": "application/json"},
            timeout=30,
        )

        # Ingest directories
        for d in dirs:
            print(f"\n[DIR] analyze_codebase('{d}')", flush=True)
            result = await _call_tool(client, session_id, "analyze_codebase", {"directory": d})
            print(f"  → {result[:200]}", flush=True)

        # Ingest loose files
        for f in files:
            print(f"\n[FILE] ingest_file('{f}')", flush=True)
            result = await _call_tool(client, session_id, "ingest_file", {"filename": f})
            print(f"  → {result[:200]}", flush=True)

        sse_task.cancel()

    print("\nDone.", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", default=SERVER)
    args = parser.parse_args()
    asyncio.run(main(args.server))
