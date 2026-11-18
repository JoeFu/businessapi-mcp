"""
Measure what a `tools/list` costs in model context under different context-size settings.

    uv run --with fastmcp tools_list_size.py

Each row starts server.py in a child process with the given env, connects in-process, and sums the JSON size of every
tool definition (name + description + input schema + annotations) — i.e. what an MCP client hands the model on connect.
Tokens are estimated at ~4 characters per token for JSON/English.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

CONFIGS = [
    ("inline schemas, all toolsets        (BAPI_INLINE_SCHEMAS=true)", {"BAPI_INLINE_SCHEMAS": "true", "BAPI_TOOLSETS": "all"}),
    ("lazy schemas (default), all toolsets", {"BAPI_INLINE_SCHEMAS": "", "BAPI_TOOLSETS": "all"}),
    ("lazy, lookup+registrations+core      (BAPI_TOOLSETS=lookup,registrations,core)", {"BAPI_INLINE_SCHEMAS": "", "BAPI_TOOLSETS": "lookup,registrations,core"}),
    ("lazy, agent_forms+core               (BAPI_TOOLSETS=agent_forms,core)", {"BAPI_INLINE_SCHEMAS": "", "BAPI_TOOLSETS": "agent_forms,core"}),
    ("lazy, monitoring+webhooks            (BAPI_TOOLSETS=monitoring,webhooks)", {"BAPI_INLINE_SCHEMAS": "", "BAPI_TOOLSETS": "monitoring,webhooks"}),
    ("lazy, lookup+ppsr                    (BAPI_TOOLSETS=lookup,ppsr)", {"BAPI_INLINE_SCHEMAS": "", "BAPI_TOOLSETS": "lookup,ppsr"}),
]

CHILD = r"""
import asyncio, json, sys
import server
from fastmcp import Client
async def main():
    async with Client(server.mcp) as c:
        tools = await c.list_tools()
        sizes = sorted(((len(json.dumps(t.model_dump(mode="json", exclude_none=True))), t.name) for t in tools), reverse=True)
        print("RESULT " + json.dumps({"tools": len(tools), "chars": sum(s for s, _ in sizes), "top": sizes[:3]}))
asyncio.run(main())
"""


def main() -> None:
    here = Path(__file__).parent
    base_env = {**os.environ, "BAPI_API_KEY": os.environ.get("BAPI_API_KEY") or "bapi_sk_test_dummy", "BAPI_ENV": "test"}
    rows = []
    for label, env in CONFIGS:
        proc = subprocess.run([sys.executable, "-X", "utf8", "-c", CHILD], env={**base_env, **env}, cwd=here, capture_output=True, text=True)
        result_line = next((ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT ")), None)
        if not result_line:
            print(f"!! {label}: child failed\n{proc.stderr[-800:]}")
            continue
        data = json.loads(result_line[len("RESULT "):])
        rows.append((label, data["tools"], data["chars"], data["chars"] // 4, data["top"]))
    width = max(len(r[0]) for r in rows)
    print(f"{'configuration':<{width}}  tools   chars   ~tokens  largest tools")
    for label, n, chars, toks, top in rows:
        largest = ", ".join(f"{name} {size:,}" for size, name in top)
        print(f"{label:<{width}}  {n:>5}  {chars:>7,}  {toks:>7,}  {largest}")


if __name__ == "__main__":
    main()
