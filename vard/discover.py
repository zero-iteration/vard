#!/usr/bin/env python3
"""VARD — AI resource discovery (stack-agnostic bootstrap).

Inspects a repo's dependencies + most-frequent call patterns, asks an LLM how THIS
codebase talks to its caches / DBs / queues, and emits a detection ruleset that the
deterministic extractor (resources.py) then applies. Runs once per repo; cached.
"""
import ast, json, os, re, sys
from collections import Counter
from . import embed as E
from .resources import DEFAULT_RULESET

MODEL = os.environ.get("VARD_LLM", "gpt-4o")
RULESET_KEYS = list(DEFAULT_RULESET.keys())


def _deps(repo):
    out = []
    for fn in ["requirements.txt", "requirements/default.txt", "requirements/base.txt",
               "pyproject.toml", "setup.py", "Pipfile", "package.json",
               "pom.xml", "build.gradle", "build.gradle.kts", "go.mod"]:
        p = os.path.join(repo, fn)
        if os.path.isfile(p):
            out.append(f"# {fn}\n" + open(p, encoding="utf-8", errors="ignore").read()[:2500])
    return "\n\n".join(out)[:6000]


def _call_patterns(repo, limit=140):
    """Multi-language: use the providers to collect receiver.method patterns + decorators."""
    from . import languages as L
    pat = Counter(); decs = Counter()
    exts = L.supported_extensions(); n = 0
    for root, dirs, files in os.walk(repo):
        dirs[:] = [d for d in dirs if d not in (".git", "node_modules", ".venv", "venv", "__pycache__", "build", "dist", "tests")]
        for f in files:
            if os.path.splitext(f)[1].lower() not in exts:
                continue
            n += 1
            if n > 1500:
                break
            prov = L.provider_for(f)
            try:
                art = prov.parse(repo, f, open(os.path.join(root, f), encoding="utf-8", errors="ignore").read())
            except Exception:
                continue
            for cs in art.calls:
                base = (cs.receiver.split(".")[-1].split("(")[0] if cs.receiver else "")
                pat[f"{base}.{cs.method}" if base else cs.method] += 1
            for s in art.symbols:
                for d in s.decorators:
                    decs[d.split("(")[0]] += 1
    top = [f"{k}  (x{v})" for k, v in pat.most_common(limit)]
    return "\n".join(top), ", ".join(f"@{k}({v})" for k, v in decs.most_common(30))


PROMPT = """You analyze a code repository to detect how it accesses CACHES, DATABASES, and QUEUES,
so a static tool can find functions that READ vs WRITE shared data resources.

DEPENDENCIES:
{deps}

DECORATORS/ANNOTATIONS seen: {imports}

MOST FREQUENT method-call patterns (receiver.method, with counts):
{patterns}

From the EVIDENCE above, return a JSON object with these keys (each a list of strings actually used by THIS repo):
- cache_receivers: variable/object names that are cache/redis/memcache clients (e.g. "redis_connection","cache")
- cache_read: method names that READ from cache (e.g. "get","hgetall")
- cache_write: method names that WRITE to cache (e.g. "set","hset","delete")
- queue_enqueue_attrs: method names that ENQUEUE async work (e.g. "delay","apply_async","enqueue")
- queue_enqueue_funcs: bare functions that enqueue (e.g. "enqueue","send_task")
- queue_decorators: decorator names marking task/consumer functions (e.g. "task","job","shared_task")
- db_read_attrs: attribute/method names for ORM reads (e.g. "query","objects","filter")
- db_model_base_markers: base class names that mark ORM models (e.g. "Model","Base")
Only include items evidenced by the dependencies/patterns. Output ONLY the JSON object."""


def build_prompt(repo):
    """The discovery prompt — give this to ANY LLM (incl. the calling agent) to get a ruleset."""
    deps, (patterns, imports) = _deps(repo), _call_patterns(repo)
    return PROMPT.format(deps=deps, imports=imports, patterns=patterns)


def save_ruleset(repo, rules):
    """Merge a discovered ruleset with defaults and cache it."""
    merged = {}
    for k in RULESET_KEYS:
        v = rules.get(k) if isinstance(rules, dict) else None
        merged[k] = sorted(set(DEFAULT_RULESET[k]) | set(v if isinstance(v, list) else []))
    out_path = os.path.join(repo, ".vard", "ruleset.json")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as _o:
        json.dump(merged, _o, indent=2)
    return merged


def discover(repo, use_cache=True, llm=None):
    """Discover the per-repo resource ruleset.
    llm: optional callable(prompt_str) -> json_str (e.g. the host AGENT). If None, use
         OPENAI_API_KEY if available, else fall back to DEFAULT_RULESET (works offline)."""
    out_path = os.path.join(repo, ".vard", "ruleset.json")
    if use_cache and os.path.isfile(out_path):
        with open(out_path) as _f:
            return json.load(_f)
    rules = {}
    if llm is not None:                                   # agent-driven discovery (explicit)
        try:
            rules = json.loads(llm(build_prompt(repo)))
        except Exception:
            rules = {}
    elif os.environ.get("VARD_DISCOVER", "").lower() == "openai":   # OPT-IN to the paid API, never silent
        print(f"→ vard: discovering resource ruleset via OpenAI ({MODEL})...", file=sys.stderr, flush=True)
        try:
            resp = E._openai_client().chat.completions.create(
                model=MODEL, response_format={"type": "json_object"}, temperature=0,
                messages=[{"role": "user", "content": build_prompt(repo)}])
            rules = json.loads(resp.choices[0].message.content)
        except Exception as e:
            print(f"  discovery failed ({str(e)[:60]}) — using built-in default ruleset", file=sys.stderr)
            rules = {}
    # else: default ruleset — free, no API call (the safe default for a key-optional tool)
    return save_ruleset(repo, rules)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(); ap.add_argument("repo"); ap.add_argument("--fresh", action="store_true")
    a = ap.parse_args()
    rs = discover(a.repo, use_cache=not a.fresh)
    print(json.dumps({k: v for k, v in rs.items()}, indent=2))
