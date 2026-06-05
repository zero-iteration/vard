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
def vard_state_candidates(task: str, repo: str) -> str:
    """STATE-FIRST localization, step 1. Returns the program's candidate STATE types (its data
    structures, narrowed to the region relevant to the task). Read them and identify which hold the
    WRONG state for this task — INCLUDING state the task doesn't name but is structurally involved
    (the config/settings/payload/entity behind the described behavior). Then call vard_state_lineage
    with those type names. Use this when a bug is about data being wrong/stale/incomplete and the
    code that sets it isn't obvious from the symptom text."""
    return cli.state_candidates_text(task, repo)


@mcp.tool()
@_safe
def vard_state_lineage(types: str, repo: str) -> str:
    """STATE-FIRST localization, step 2. Given state type names you identified (comma/space
    separated, e.g. "CreationSettings, MockNameImpl"), returns the code that DEFINES and
    PRODUCES/CONSUMES that state: the type definitions, their members, the methods that build/read
    them, and interface/impl. This is where a 'wrong state' bug is actually fixed — including
    producers in other modules that have no textual link to the symptom."""
    return cli.state_lineage_text(types, repo)


@mcp.tool()
@_safe
def vard_config(query: str, repo: str) -> str:
    """Find the config/properties that change behaviour at RUNTIME — the settings that aren't in the code.
    Given a key, a key fragment, or a symbol name, returns where each config key is DEFINED
    (file:line = value, across all profiles like application.yml / application-prod.yml) and the CODE that
    READS it (`@Value("${...}")`, `getProperty`, `os.getenv`, `process.env`...). Use this when behaviour
    depends on configuration (a feature flag, cache TTL, datasource, profile override) rather than code —
    it surfaces a value→code coupling that has no call/import link. Does NOT claim which value wins at
    runtime (that depends on the active profile/env); it shows all definitions with their source."""
    return cli.config_text(query, repo)


@mcp.tool()
@_safe
def vard_remember(fact: str, citations: str, repo: str, reason: str = "") -> str:
    """Persist a durable fact about this repo that is NOT in the code — a decision, constraint, gotcha,
    or correction the user told you (e.g. "this cache is the source of truth, not the DB"; "never call X
    directly, it skips validation"). `citations` = comma-separated code anchors the fact is about (a
    symbol like "RedisCacheManager.createConfig" or a "file.py:line"). The fact is ANCHORED to that code
    and auto-invalidated when that code changes, so it can't silently go stale. A fact with no resolvable
    citation is refused (unanchorable claims can't be verified). Call this when the user states something
    durable about how/why the code works that future sessions should not have to be re-told."""
    return cli.remember_text(fact, citations, repo, reason=reason)


@mcp.tool()
@_safe
def vard_recall(task: str, repo: str) -> str:
    """Recall durable facts previously remembered about the code relevant to `task` — decisions,
    constraints, gotchas the user stated that aren't visible in the code. Each is freshness-checked
    against the current code: ✓ = still valid, ⚠ = the cited code changed since (re-check before relying
    on it). Call this before answering questions about how/why code behaves, to avoid contradicting what
    the user already told you."""
    return cli.recall_text(task, repo)


@mcp.tool()
@_safe
def vard_whole_picture(target: str, repo: str) -> str:
    """THE WHOLE PICTURE before you touch a file/symbol — call this before editing or when you need
    full context, not just the code. Given a class or file (e.g. "UserServiceImpl"), returns one
    joined answer: the code here, the STATE/data it touches, the code COUPLED through shared
    data (what you'd break), the DECISIONS / TICKETS / INCIDENTS behind it (why it's this way, mined
    from history), and the files that historically CHANGE TOGETHER with it. This is context you
    cannot reconstruct by reading the code — the 'why', the hidden couplings, the history."""
    return cli.whole_picture_text(target, repo)


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
