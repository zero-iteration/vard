#!/usr/bin/env python3
"""Config/properties layer — index the declarative state that changes behaviour at RUNTIME but is invisible
to a code-only graph. A config key is the ultimate distant coupling: defined in application.yml, read by
code three modules away via @Value("${...}"), with no call/import edge. We model it like any resource:
the config file is the DEFINITION site, the code that reads the key is the READER.

  build_config_index(rg, repo) -> { key: {"defs":[{key,value,file,line}], "readers":[node_id]} }

Honest scope: we surface ALL definitions (e.g. application.yml AND application-prod.yml) with their source;
we never claim which value wins at runtime (that depends on the active profile/env we can't know).
"""
import os, re
from . import state as ST

_CFG_EXT = (".properties", ".yml", ".yaml", ".env")
_SKIP_DIR = {".git", "target", "build", "node_modules", ".venv", ".vard", ".idea", "dist", "__pycache__"}
_PROP_LINE = re.compile(r'^\s*([A-Za-z0-9_.\-]+)\s*[=:]\s*(.*?)\s*$')

# code -> config-key reads, applied PER LANGUAGE (a `${...}` is a config placeholder in Java but a string
# template in JS/Vue — applying it everywhere produced junk keys like `i`). Patterns chosen by file ext.
_P_PLACEHOLDER = re.compile(r'\$\{\s*([A-Za-z0-9_.\-]+)\s*(?::[^}]*)?\}')      # Spring ${key} / ${key:default}
_P_GETPROP = re.compile(r'get(?:Required)?Property\(\s*["\']([A-Za-z0-9_.\-]+)["\']')
_P_JAVA_ENV = re.compile(r'System\.get(?:env|Property)\(\s*["\']([A-Za-z0-9_.\-]+)["\']')
_P_PY = re.compile(r'os\.(?:getenv\(|environ(?:\.get)?\[?)\s*["\']([A-Za-z0-9_.\-]+)["\']')
_P_JS = re.compile(r'process\.env(?:\.([A-Za-z0-9_]+)|\[\s*["\']([A-Za-z0-9_.\-]+)["\'])')
_P_GO = re.compile(r'(?:os\.Getenv|viper\.Get\w*)\(\s*["\']([A-Za-z0-9_.\-]+)["\']')

_READ_BY_EXT = {
    ".java": [_P_PLACEHOLDER, _P_GETPROP, _P_JAVA_ENV], ".kt": [_P_PLACEHOLDER, _P_GETPROP, _P_JAVA_ENV],
    ".py": [_P_PY], ".pyi": [_P_PY],
    ".js": [_P_JS], ".jsx": [_P_JS], ".ts": [_P_JS], ".tsx": [_P_JS], ".mjs": [_P_JS], ".cjs": [_P_JS],
    ".go": [_P_GO],
}


def _norm(key):
    """Canonical key for matching across yaml-dotted / ENV_UPPER_SNAKE / kebab. (Spring relaxed binding.)"""
    return key.strip().lower().replace("_", ".").replace("-", ".")


def _parse_props(path):
    out = []
    try:
        for i, line in enumerate(open(path, encoding="utf-8", errors="ignore"), 1):
            if line.lstrip().startswith(("#", "!")):
                continue
            m = _PROP_LINE.match(line)
            if m and m.group(1):
                out.append((m.group(1), m.group(2), i))
    except Exception:
        pass
    return out


def _parse_yaml(path):
    """Light indent-based nested-key flattener (no PyYAML dependency). Handles the common
    `a:\\n  b:\\n    c: val` case; ignores list items and block scalars."""
    out, stack = [], []           # stack of (indent, key)
    try:
        for i, raw in enumerate(open(path, encoding="utf-8", errors="ignore"), 1):
            if not raw.strip() or raw.lstrip().startswith(("#", "-")):
                continue
            indent = len(raw) - len(raw.lstrip(" "))
            m = re.match(r'\s*([A-Za-z0-9_.\-]+)\s*:\s*(.*?)\s*$', raw)
            if not m:
                continue
            key, val = m.group(1), m.group(2)
            while stack and stack[-1][0] >= indent:
                stack.pop()
            stack.append((indent, key))
            if val and not val.startswith(("#", "|", ">", "&", "*")):
                dotted = ".".join(k for _, k in stack)
                out.append((dotted, val.strip('"\''), i))
    except Exception:
        pass
    return out


def scan_config_defs(repo):
    """{norm_key: [{key,value,file,line}]} over all config files in the repo."""
    repo = os.path.abspath(repo)
    defs = {}
    for dp, dirs, files in os.walk(repo):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIR]
        for f in files:
            ext = os.path.splitext(f)[1].lower()
            if ext not in _CFG_EXT:
                continue
            path = os.path.join(dp, f)
            rel = os.path.relpath(path, repo)
            entries = _parse_yaml(path) if ext in (".yml", ".yaml") else _parse_props(path)
            for key, val, line in entries:
                defs.setdefault(_norm(key), []).append(
                    {"key": key, "value": (val or "")[:120], "file": rel, "line": line})
    return defs


def scan_config_reads(rg, repo):
    """{norm_key: set(node_id)} — code symbols that read a config key. Patterns are chosen by the file's
    language, so a JS string template `${x}` is not mistaken for a Spring config placeholder."""
    cache, reads = {}, {}
    for n in ST._content_nodes(rg):
        pats = _READ_BY_EXT.get(os.path.splitext(n.file)[1].lower())
        if not pats:
            continue
        txt = ST._node_text(repo, n, cache)
        for rx in pats:
            for m in rx.findall(txt):
                key = m if isinstance(m, str) else next((g for g in m if g), "")  # _P_JS has 2 groups
                if key and len(key) >= 2:
                    reads.setdefault(_norm(key), set()).add(n.id)
    return reads


_CP_PREFIX = re.compile(r'ConfigurationProperties\(\s*(?:(?:prefix|value)\s*=\s*)?["\']([^"\']+)["\']')


def _prefix_bound_reads(rg, defs):
    """Spring @ConfigurationProperties(prefix="x") binds the WHOLE `x.*` config subtree to a class with no
    @Value per key. Couple every defined key under the prefix to that class — the dominant Spring mechanism
    that per-key read detection misses entirely."""
    extra = {}
    for nid, decs in getattr(rg, "node_decorators", {}).items():
        for d in decs:
            if "ConfigurationProperties" not in d:
                continue
            m = _CP_PREFIX.search(d)
            if not m:
                continue
            pfx = _norm(m.group(1))
            for k in defs:
                if k == pfx or k.startswith(pfx + "."):
                    extra.setdefault(k, set()).add(nid)
    return extra


def build_config_index(rg, repo):
    """Store only the CODE-RELEVANT keys — those actually read in code (per-key via @Value/${}/getenv, OR
    bound by @ConfigurationProperties prefix), coupled or read-but-undefined. The 1000s of defined-but-unread
    keys (k8s manifests, test fixtures, framework defaults) are noise for a code↔config coupling layer;
    arbitrary 'where is key X defined' lookups re-scan config files on demand."""
    defs = scan_config_defs(repo)
    reads = scan_config_reads(rg, repo)
    for k, ids in _prefix_bound_reads(rg, defs).items():     # add @ConfigurationProperties subtree bindings
        reads.setdefault(k, set()).update(ids)
    out = {}
    for k in reads:
        out[k] = {"defs": defs.get(k, []), "readers": sorted(reads[k])}
    return out


# ---- query side -------------------------------------------------------------

def _match(q, nq, k, defs):
    # exact, dotted-prefix either way, or a long-enough substring (avoids 1-2 char keys matching everything)
    return (nq == k or k.startswith(nq + ".") or nq.startswith(k + ".")
            or (len(nq) >= 4 and nq in k) or any(q.lower() in d["key"].lower() for d in defs if len(q) >= 4))


def lookup(cfg, rg, query, repo=None):
    """Config keys relevant to `query` (a key, a substring, or a symbol name whose code reads keys).
    The stored index holds only code-read keys; for an arbitrary defined-but-unread key, re-scan on demand."""
    cfg = cfg or {}
    q = query.strip(); nq = _norm(q)
    hits = [(k, v) for k, v in cfg.items() if _match(q, nq, k, v["defs"])]
    if not hits:                                   # try: a symbol whose code reads config
        ids = {n.id for n in rg.nodes.values() if q in n.qual}
        hits = [(k, v) for k, v in cfg.items() if ids & set(v["readers"])]
    if not hits and repo:                          # fallback: defined-but-unread key, scanned live
        for k, dl in scan_config_defs(repo).items():
            if _match(q, nq, k, dl):
                hits.append((k, {"defs": dl, "readers": []}))
    return hits


def render(cfg, rg, query, repo=None, limit=25):
    hits = lookup(cfg, rg, query, repo=repo)
    if not hits:
        return f"# No config keys match '{query}'."
    out = [f"# Config keys for: {query}"]
    for k, v in hits[:limit]:
        out.append(f"\n## {v['defs'][0]['key'] if v['defs'] else k}")
        if v["defs"]:
            for d in v["defs"]:
                out.append(f"  defined: {d['file']}:{d['line']}  = {d['value']}")
        else:
            out.append("  (read in code but NOT defined in any config file — likely set via env/secret at runtime)")
        rd = [rg.nodes[i] for i in v["readers"] if i in rg.nodes]
        if rd:
            out.append("  read by:")
            for n in rd[:8]:
                out.append(f"    - {n.file}:{n.start}-{n.end}  {n.qual.split('::')[-1]}")
        elif v["defs"]:
            out.append("  (defined but no code reader found — possibly dead config)")
    return "\n".join(out)


def for_nodes(cfg, rg, node_ids, limit=8):
    """Config keys read by the given code nodes — for folding into context (the runtime values it depends on)."""
    if not cfg:
        return []
    node_ids = set(node_ids)
    rows = []
    for k, v in cfg.items():
        if set(v["readers"]) & node_ids and v["defs"]:
            d = v["defs"][0]
            rows.append(f"{d['key']} = {d['value']}  ({d['file']}:{d['line']}"
                        + (f", +{len(v['defs'])-1} more def" if len(v["defs"]) > 1 else "") + ")")
    return rows[:limit]
