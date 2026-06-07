#!/usr/bin/env python3
"""State-first localization layer.

The program's STATE (types/fields — the data it's about) is the skeleton; code hangs off it as
"what produces / consumes / mutates this state". To localize a task: identify the implicated state,
then return the code that defines and produces it. Coupling (a state written by more than one place)
falls out as one case — it is not special.

The state graph is built once at index time (build_state_graph, stored in the index) so queries are
instant. Two query paths:
  - auto_section(): zero-shot, GATED — fires only when state is clearly implicated (a type named in
    the task, or a cache/queue resource the task points at). Precise; used by `vard context`.
  - candidates()/lineage(): the agent names the implicated state, VARD traverses it. This is the
    strong path for state whose name the task never mentions (validated: agent identification recovers
    dark state that heuristics/embeddings cannot — see eval/FINDINGS.md).
"""
import os, re
from .languages.profiles import dominant_profile

_CAP = re.compile(r'\b([A-Z][A-Za-z0-9_]+)\b')
# query-text keywords (match the user's ISSUE text, language-neutral) — these stay here, not in a profile
_CACHE_Q = re.compile(r'\b(cache|cached|caching|redis|seriali|deserial|jackson|round-?trip|wrapper)\b', re.I)
_QUEUE_Q = re.compile(r'\b(kafka|queue|topic|consum|publish|listener|message|event|broker|rabbit|rocket)\b', re.I)
_MAX_REFS = 40
_GOD_PRODUCER_CAP = 25
# All language-/framework-specific patterns (infra/data naming, construction, annotations, field decls,
# return-type & mutation parsing) live in vard/languages/profiles.py, resolved per-repo by dominant_profile.
# COMPAT SHIMS (Java-default): a few eval-harness modules (eval/edges2, memory_db, identify, valueflow) read
# these module-level regexes directly. The eval corpus is all-Java, so the Java profile's patterns are the
# correct default for them. Production code paths (build_state_graph / candidates / auto_implicated / memory)
# resolve the per-repo profile instead and do NOT use these.
from .languages.profiles import JavaProfile as _JavaProfile
_INFRA = _JavaProfile.infra_re
_DATA_LIKE = _JavaProfile.data_like_re
_NONDATA = _JavaProfile.nondata_re


def _content_nodes(rg):
    return [n for n in rg.nodes.values() if n.type in ("function", "method", "class")]


def _src(repo, rel, cache):
    if rel not in cache:
        try:
            with open(os.path.join(repo, rel), encoding="utf-8", errors="ignore") as _f:
                cache[rel] = _f.read().splitlines()
        except Exception:
            cache[rel] = []
    return cache[rel]


def _node_text(repo, n, cache):
    return "\n".join(_src(repo, n.file, cache)[n.start - 1:n.end])


def _decl_text(repo, n, cache):
    return " ".join(_src(repo, n.file, cache)[n.start - 1:n.start + 8])


def build_state_graph(rg, repo):
    """Compute the state graph from the symbol graph + on-disk source. Stored in the index.
    Returns a picklable dict: type_def, type_refs, up/down (interface<->impl), resources, holders.
    Language-/framework-specific signals come from the repo's dominant LanguageProfile."""
    prof = dominant_profile(rg)
    content = _content_nodes(rg)
    type_def = {}
    for n in rg.nodes.values():
        if n.type == "class":
            type_def.setdefault(n.name, []).append(n.id)
    typenames = set(type_def)

    up, down = {}, {}
    for u, v, k in rg.G.edges(keys=True):
        if k == "inherits":
            up.setdefault(u, set()).add(v)
            down.setdefault(v, set()).add(u)

    cache, type_refs, producers, resources, holders = {}, {}, {}, [], {}
    deco = getattr(rg, "node_decorators", {})
    for n in content:
        txt = _node_text(repo, n, cache)
        decl = _decl_text(repo, n, cache)
        refs = {t for t in _CAP.findall(txt) if t in typenames and t != n.name}
        for t in refs:
            type_refs.setdefault(t, set()).add(n.id)
        # producers of a type: nodes that construct / build / return / mutate it (the "writers" of state)
        prod = ({t for t in prof.new_re.findall(txt) if t in typenames}
                | {t for t in prof.builder_re.findall(txt) if t in typenames}
                | prof.return_type_types(decl, n.name, typenames)
                | prof.mutated_types(txt, typenames))
        for t in prod:
            if t != n.name:
                producers.setdefault(t, set()).add(n.id)
        anns = " ".join(deco.get(n.id, []))
        if prof.cache_ann_re.search(anns):
            st = {t for t in prof.return_type_types(decl, n.name, typenames) if not prof.infra_re.search(t)}
            resources.append((n.id, "cache", sorted(st)))
        elif prof.queue_ann_re.search(anns):
            resources.append((n.id, "queue", sorted({t for t in refs if not prof.infra_re.search(t)})))
        if n.type == "class":
            real = [(nm, ty) for nm, ty in prof.static_fields(txt)
                    if not prof.logger_re.search(ty) and not nm.isupper()]
            if real:
                holders[n.id] = sorted({ty for _, ty in real if ty in typenames and not prof.infra_re.search(ty)} | {n.name})
    # store sets as sorted lists for clean pickling
    return {"type_def": type_def,
            "type_refs": {t: sorted(ids) for t, ids in type_refs.items()},
            "producers": {t: sorted(ids) for t, ids in producers.items()},
            "up": {k: sorted(v) for k, v in up.items()},
            "down": {k: sorted(v) for k, v in down.items()},
            "resources": resources, "holders": holders}


# ---- query side -------------------------------------------------------------

def _type_closure(sg, name, rg):
    out = set()
    for did in sg["type_def"].get(name, []):
        out.add(did)
        out |= set(sg["up"].get(did, [])) | set(sg["down"].get(did, []))
        out |= {v for _, v, k in rg.G.out_edges(did, keys=True) if k == "contains"}
    refs = sg["type_refs"].get(name, [])
    if len(refs) <= _MAX_REFS:
        out |= set(refs)                                   # ordinary type: all referencers
    else:
        # god-type (referenced everywhere): don't drop everything — keep the PRODUCERS (writers of
        # this state), which is what a 'wrong state' bug needs. Resource-touching producers (those that
        # store/derive the state into a cache/queue) are the real state-writers: keep ALL of them, never
        # capped. Only the low-signal remaining producers are bounded (and the cap scales with fan-in).
        prods = sg.get("producers", {}).get(name, [])
        res_ids = {nid for nid, _, _ in sg.get("resources", [])}
        strong = [p for p in prods if p in res_ids]
        weak = sorted(p for p in prods if p not in res_ids)
        cap = max(_GOD_PRODUCER_CAP, len(refs) // 4)       # scale with the type's fan-in, floor 25
        out |= set(strong) | set(weak[:cap])
    return out


def lineage(sg, rg, type_names):
    """Traverse the named state types -> their def + members + producers/consumers + interface/impl."""
    ids = set()
    for t in type_names:
        if t in sg["type_def"]:
            ids |= _type_closure(sg, t, rg)
    return ids


def candidates(sg, rg, seed_files=None, task="", max_n=400):
    """The state types the agent chooses from. For big repos, narrowed to types whose def lives in or
    near (1 import hop) the files the search surfaced — structural, so textually-disconnected state is
    kept. seed_files: files of the content-top hits."""
    prof = dominant_profile(rg)
    names = {t for t in sg["type_def"] if not prof.infra_re.search(t)}
    if len(names) > max_n and seed_files:
        near = {t for t in names for did in sg["type_def"].get(t, []) if rg.nodes[did].file in seed_files}
        names = near or names

    def _paths(t):
        return [rg.nodes[d].file.lower() for d in sg["type_def"].get(t, [])]

    def is_data(t):
        return bool(prof.data_like_re.search(t)) or any(s in p for p in _paths(t) for s in prof.data_dirs)

    def is_svc(t):
        return bool(prof.nondata_re.search(t)) or any(s in p for p in _paths(t) for s in prof.svc_dirs)

    # rank: actual STATE (data POJOs/DTOs/entities, by name OR package) first; service/infra last
    def rank(t):
        return (0 if is_data(t) else (2 if is_svc(t) else 1), t)
    return sorted(names, key=rank)[:max_n]


def auto_implicated(sg, rg, task, seed_ids):
    """GATED implication for zero-shot use: types NAMED in the task + state shapes of cache/queue
    resources the task points at. Deliberately omits the broad seed-reference path (it adds noise on
    ordinary bugs); the agent path covers state the task doesn't name."""
    from . import common as C
    prof = dominant_profile(rg)
    idents, _ = C.extract_seeds(task)
    qtok = set(C.subtokens(task))
    seedset, seed_files = set(seed_ids), {rg.nodes[i].file for i in seed_ids}
    out = set()
    for t in idents:
        if t in sg["type_def"] and not prof.infra_re.search(t):
            out.add(t)
    kinds = set()
    if _CACHE_Q.search(task or ""):
        kinds.add("cache")
    if _QUEUE_Q.search(task or ""):
        kinds.add("queue")
    for nid, kind, stypes in sg["resources"]:
        n = rg.nodes.get(nid)
        if n is None:
            continue
        if kind in kinds or nid in seedset or n.file in seed_files or (qtok & set(C.subtokens(n.qual))):
            out |= set(stypes)
    for hid, stypes in sg["holders"].items():
        if hid in seedset or rg.nodes.get(hid) and rg.nodes[hid].file in seed_files:
            out |= set(stypes)
    if not out:
        # fallback so the zero-shot section isn't empty: the data-like types the content seeds
        # reference (capped, data-only — stays precise, doesn't flood on logic bugs).
        scored = {}
        for t, refs in sg["type_refs"].items():
            if prof.data_like_re.search(t) and not prof.infra_re.search(t):
                hits = len(set(refs) & seedset)
                if hits:
                    scored[t] = hits
        out |= set(sorted(scored, key=scored.get, reverse=True)[:3])
    return out


def render(rg, ids, exclude=None):
    exclude = exclude or set()
    lines = []
    for nid in sorted(ids):
        if nid in exclude or nid not in rg.nodes:
            continue
        n = rg.nodes[nid]
        lines.append(f"- {n.file}:{n.start}-{n.end}  {n.qual}")
    return lines
