#!/usr/bin/env python3
"""
VARD MCP server — plugs repository-attention + data-coupling retrieval into any
MCP agent (Claude Code, Cursor, ...). Stdio transport.

Tools:
  vard_context(task, repo)        -> relevant code + data-coupled partners (the main one)
  vard_couplings(repo)            -> hidden writer⇄reader couplings through cache/db/queue
  vard_index(repo)                -> (re)build the index; key-free by default
  vard_discovery_request(repo)    -> returns a prompt for AGENT-DRIVEN discovery (no API key)
  vard_set_ruleset(repo, json)    -> agent submits the ruleset it produced; reindexes

The discovery_request/set_ruleset pair lets the calling agent BE the model: it reads
the prompt, reasons in its own context, and hands back the ruleset — no OpenAI key.
"""
import functools, json, os, traceback
from mcp.server.fastmcp import FastMCP
from . import cli, discover as D

mcp = FastMCP("vard")


def _safe(fn):
    """An agent must get a readable error string, never a crashed tool call."""
    @functools.wraps(fn)
    def wrap(*a, **k):
        try:
            return fn(*a, **k)
        except Exception as e:
            return (f"vard error in {fn.__name__}: {type(e).__name__}: {str(e)[:300]}\n"
                    + (traceback.format_exc()[-800:] if os.environ.get("VARD_DEBUG") else
                       "(the repo may not be indexable, or a dependency is missing — try `vard init <repo>` in a shell)"))
    return wrap


@mcp.tool()
@_safe
def vard_context(task: str, repo: str, k: int = 8, hypothetical: str = "") -> str:
    """Retrieve code relevant to a task/bug, INCLUDING functions coupled through shared
    data (cache/DB/queue) and files that similar past changes touched — context grep and
    embeddings miss. Index auto-builds/refreshes only on change (instant otherwise).
    TIP: if you can guess what the relevant code looks like, pass a short hypothetical code
    snippet as `hypothetical` (function/method names, key calls). It bridges the
    description→code vocabulary gap and sharply improves recall on behavioral issues (HyDE)."""
    return cli.context_text(task, repo, k, hypothetical or None)


@mcp.tool()
@_safe
def vard_couplings(repo: str, limit: int = 40) -> str:
    """List implicit data couplings (writer⇄reader through a shared cache key / DB model /
    queue) — links with no call, import, shared text, or semantic similarity."""
    return cli.couplings_text(repo, limit)


@mcp.tool()
@_safe
def vard_impact(target: str, repo: str) -> str:
    """BLAST RADIUS before an edit. Given a function/method/class (qualified name or
    file:line), return everything affected if you change it — code coupled through shared
    caches/DBs/queues (downstream readers of what it writes, upstream writers it depends on),
    plus same-class siblings and likely callers, each with a reason. CALL THIS BEFORE EDITING
    code that touches shared state, to avoid breaking a hidden reader/writer on the other side."""
    return cli.impact_text(target, repo)


@mcp.tool()
@_safe
def vard_resource(name: str, repo: str) -> str:
    """Who reads and writes a given data resource (cache key / DB table-or-model / queue).
    Pass a substring like 'order', 'redis:status', 'table:user', 'queue:orders'. Useful for
    debugging 'stale data' or 'message not processed' bugs — connects writers to readers
    across modules in a way grep cannot."""
    return cli.resource_text(name, repo)


@mcp.tool()
@_safe
def vard_index(repo: str, fresh: bool = False) -> str:
    """Build/refresh the VARD index (attention graph + data-resource layer). Key-free by default."""
    s = cli.build_index(repo, fresh=fresh)
    return json.dumps(s, indent=2)


@mcp.tool()
@_safe
def vard_discovery_request(repo: str) -> str:
    """AGENT-DRIVEN discovery (no API key): returns a prompt describing this repo's deps and
    call patterns. YOU (the agent) answer it with a JSON ruleset, then call vard_set_ruleset."""
    return D.build_prompt(repo)


@mcp.tool()
@_safe
def vard_set_ruleset(repo: str, ruleset_json: str) -> str:
    """Submit the ruleset you produced from vard_discovery_request; merges, caches, reindexes."""
    try:
        rules = json.loads(ruleset_json)
    except Exception as e:
        return f"invalid JSON: {e}"
    D.save_ruleset(os.path.abspath(repo), rules)
    s = cli.build_index(repo, fresh=False)   # reuses cached ruleset.json
    return f"ruleset saved + reindexed: {json.dumps(s)}"


def main():
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
