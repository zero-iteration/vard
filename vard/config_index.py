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

# code -> config-key reads (the dominant frameworks; same shape as the resource ruleset)
_READ_PATTERNS = [
    re.compile(r'\$\{\s*([A-Za-z0-9_.\-]+)\s*(?::[^}]*)?\}'),          # Spring/${...} placeholders (incl :default)
    re.compile(r'getProperty\(\s*["\']([A-Za-z0-9_.\-]+)["\']'),       # Environment.getProperty("key")
    re.compile(r'getRequiredProperty\(\s*["\']([A-Za-z0-9_.\-]+)["\']'),
    re.compile(r'System\.getenv\(\s*["\']([A-Za-z0-9_.\-]+)["\']'),    # java env
    re.compile(r'System\.getProperty\(\s*["\']([A-Za-z0-9_.\-]+)["\']'),
    re.compile(r'os\.getenv\(\s*["\']([A-Za-z0-9_.\-]+)["\']'),        # python
    re.compile(r'os\.environ(?:\.get)?\[?\s*["\']([A-Za-z0-9_.\-]+)["\']'),
    re.compile(r'process\.env\.([A-Za-z0-9_]+)'),                      # js/ts
    re.compile(r'process\.env\[\s*["\']([A-Za-z0-9_.\-]+)["\']'),
    re.compile(r'os\.Getenv\(\s*["\']([A-Za-z0-9_.\-]+)["\']'),        # go
    re.compile(r'viper\.Get\w*\(\s*["\']([A-Za-z0-9_.\-]+)["\']'),
]


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
    """{norm_key: set(node_id)} — code symbols that read a config key."""
    cache, reads = {}, {}
    for n in ST._content_nodes(rg):
        txt = ST._node_text(repo, n, cache)
        if "${" not in txt and "getenv" not in txt.lower() and "getproperty" not in txt.lower() \
           and "process.env" not in txt and "os.environ" not in txt and "viper.get" not in txt.lower():
            continue
        for rx in _READ_PATTERNS:
            for key in rx.findall(txt):
                reads.setdefault(_norm(key), set()).add(n.id)
    return reads


def build_config_index(rg, repo):
    defs = scan_config_defs(repo)
    reads = scan_config_reads(rg, repo)
    out = {}
    for k in set(defs) | set(reads):
        out[k] = {"defs": defs.get(k, []), "readers": sorted(reads.get(k, set()))}
    return out


# ---- query side -------------------------------------------------------------

def lookup(cfg, rg, query):
    """Config keys relevant to `query` (a key, a substring, or a symbol name whose code reads keys)."""
    if not cfg:
        return []
    q = query.strip()
    nq = _norm(q)
    hits = []
    for k, v in cfg.items():
        if nq == k or nq in k or k in nq or any(q.lower() in d["key"].lower() for d in v["defs"]):
            hits.append((k, v))
    if not hits:                                   # try: a symbol that reads config
        ids = {n.id for n in rg.nodes.values() if q in n.qual}
        for k, v in cfg.items():
            if ids & set(v["readers"]):
                hits.append((k, v))
    return hits


def render(cfg, rg, query, limit=25):
    hits = lookup(cfg, rg, query)
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
