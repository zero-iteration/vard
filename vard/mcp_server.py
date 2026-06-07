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
def vard_context(task: str, repo: str, k: int = 8, hypothetical: str = "", runtime_mode: str = "") -> str:
    """Retrieve code relevant to a task/bug, INCLUDING functions coupled through shared
    data (cache/DB/queue) and files that similar past changes touched — context grep and
    embeddings miss. Index auto-builds/refreshes only on change (instant otherwise).
    TIP: if you can guess what the relevant code looks like, pass a short hypothetical code
    snippet as `hypothetical` (function/method names, key calls). It bridges the
    description→code vocabulary gap and sharply improves recall on behavioral issues (HyDE).
    runtime_mode (off/fused/prior/tag, default auto): how to use the runtime overlay from `vard test` —
    'fused' amplifies code observed running + real-call-graph proximity; 'off' is the pure static ranking;
    'tag' shows what ran without changing the order. Runtime can only promote (never hurts recall)."""
    return cli.context_text(task, repo, k, hypothetical or None, runtime_mode=runtime_mode or None)


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
def vard_candidates(task: str, repo: str) -> str:
    """The RECALL-COMPLETE candidate pool for a task — every candidate tagged with WHY it's here
    (content / resource-coupled / state-producer / import-1hop / co-changed×N / config-anchor /
    package-sibling). Use this instead of vard_context when you need high recall and don't want to miss
    code coupled through shared data/state with no textual or call link (the dark-coupling case): it
    surfaces a superset, tagged, and YOU pick the relevant ones — recall from the pool, precision from you.
    Larger output than vard_context; reach for it on hard "what else touches this?" / wrong-stale-data bugs."""
    return cli.candidates_text(task, repo)


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
def vard_remember(fact: str, citations: str, repo: str, reason: str = "", kind: str = "mechanism") -> str:
    """Persist a durable fact about this repo that is NOT in the code — a decision, constraint, gotcha,
    or correction the user told you (e.g. "this cache is the source of truth, not the DB"; "never call X
    directly, it skips validation"). `citations` = comma-separated code anchors the fact is about (a
    symbol like "RedisCacheManager.createConfig" or a "file.py:line"). The fact is ANCHORED to that code
    and auto-invalidated when that code changes, so it can't silently go stale. A fact with no resolvable
    citation is refused (unanchorable claims can't be verified). `kind` ∈ {mechanism, expectation,
    observation}: mechanism = WHY it's coded this way (default); expectation = what the user EXPECTED/intended
    (use vard_expect); observation = something noted as seen. The kind decides which side of the
    actual-vs-expected join (vard_explain) the fact feeds. Call this when the user states something durable
    about how/why the code works that future sessions should not have to be re-told."""
    return cli.remember_text(fact, citations, repo, reason=reason, kind=kind)


@mcp.tool()
@_safe
def vard_expect(expectation: str, citations: str, repo: str, reason: str = "") -> str:
    """Record what the user EXPECTED the code to do — the oracle side of vard_explain. Use this whenever the
    user states intended behavior or corrects an assumption ("the cheaper option should win"; "after an edit
    the user must still be able to log in"). `citations` = the code anchors it's about. vard_explain then
    contrasts this EXPECTED behavior against what ACTUALLY runs (the runtime overlay) and flags the divergence
    — e.g. "you expected X at method M, but M was never observed running." Anchored + freshness-checked like
    any memory, so a changed code path flags the expectation as needing re-confirmation."""
    return cli.expect_text(expectation, citations, repo, reason=reason)


@mcp.tool()
@_safe
def vard_explain(target: str, repo: str) -> str:
    """THE confident answer: how the code ACTUALLY runs vs what you EXPECTED, with the divergence made
    explicit. Given a symbol, file, or ticket id, returns one joined, provenance-tagged answer:
      ACTUAL      — methods/edges OBSERVED running (the runtime overlay from `vard test`)  [confirmed-runtime]
      MECHANISM   — the code + the commit/ticket that introduced it                        [code]/[commit]
      EXPECTED    — what the user told us to expect (vard_expect) + ticket text             [your expectation]
      CONFIG      — the settings that steer it (file-values across profiles)               [config]
      DIVERGENCE  — explicit conflicts (expected-but-not-observed, stale expectation,
                    config-profile ambiguity)                                              [divergence]
      UNCERTAINTY — what could NOT be confirmed (never guessed)                            [unverified]
    It never claims to find the bug; it makes the actual-vs-expected divergence undeniable, with every claim
    tagged by how it's known. Use for "why does X behave this way / why is the prod behavior wrong" questions,
    especially after `vard test` has grounded the ACTUAL leg."""
    return cli.explain_text(target, repo)


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
def vard_coverage(target: str, repo: str) -> str:
    """Did a method actually run, or is its absence a gap? For a function/method, returns one of: EXECUTED
    (observed on a traced path), INSTRUMENTED-but-never-ran (it's reachable to the agent but no traced
    request/test hit it — drive it), or NOT-instrumented (a real gap — class not loaded / outside the trace
    scope / transform failed). Use this when runtime/explain doesn't show a method you expected — it tells
    you whether to drive a different path or whether instrumentation missed it, instead of guessing."""
    return cli.coverage_text(target, repo)


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


def _reap_on_parent_death():
    """Exit when our parent (the MCP client) dies — stdio servers can be left orphaned on client
    disconnect, leaking processes across a session. A reparented process (getppid → 1) means the
    parent is gone; bail immediately so we don't linger."""
    import threading, time
    ppid0 = os.getppid()
    def watch():
        while True:
            time.sleep(5)
            try:
                if os.getppid() != ppid0:      # reparented to init → original parent died
                    os._exit(0)
            except Exception:
                os._exit(0)
    t = threading.Thread(target=watch, name="vard-mcp-reaper", daemon=True)
    t.start()


def main():
    _reap_on_parent_death()
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
