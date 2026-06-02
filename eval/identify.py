"""Agent-identifies-state -> VARD-traverses.

The hard part (symptom -> which state is wrong) is REASONING, so an LLM does it; VARD holds the
state skeleton and traverses. This module:
  - candidates(idx): the state nodes (types) the identifier chooses from. For big repos it is
    structurally narrowed (types within 2 import hops of the content seeds) — STRUCTURAL proximity,
    not lexical, so textually-disconnected dark state (e.g. CreationSettings near mock-creation code)
    is kept while the long tail is cut.
  - save/load_identified: the agent's chosen type names, keyed by the task text.
  - agent_state_closure: VARD traverses the identified types' lineage (def + members + producers/
    consumers + interface/impl).
"""
import json, os, hashlib
from vard import rank as RK, selflabel as SL, propagate as P, common as C
from . import state_lineage as SLG, channels as CH

IDENT_FILE = os.path.expanduser("~/.vard-eval/identified.json")


def _key(task):
    return hashlib.md5((task or "")[:300].encode()).hexdigest()[:12]


def candidate_types(idx, repo, task, max_n=450):
    sg = SLG.build_state_graph(idx, repo)
    rg = idx["rg"]
    allnames = sorted({t for t in sg["type_def"] if not SLG._INFRA.search(t)})
    if len(allnames) <= max_n:
        return allnames, sg
    nodes = CH.content_nodes(rg)
    chunks, keys, bm = CH.chunk_index(nodes, repo)
    lex = CH.topk_ids(CH.lexical_scores(task, keys, bm), 20)
    sem = CH.topk_ids(CH.semantic_scores(task, nodes, keys, chunks, repo), 20)
    seed_files = {rg.nodes[i].file for i in (lex | sem)}
    reach = CH.structural_reach_files(idx, seed_files, hops=2) | seed_files
    near = {t for t in allnames for did in sg["type_def"].get(t, []) if rg.nodes[did].file in reach}
    return sorted(near)[:max_n], sg


def _mm(d):
    if not d:
        return {}
    v = list(d.values()); lo, hi = min(v), max(v)
    return {k: (x - lo) / (hi - lo) if hi > lo else 0.0 for k, x in d.items()}


def softmax_identify(idx, task, repo, topn=10):
    """The probabilistic / 'attention' identifier: score each candidate STATE TYPE by the local
    features available (content similarity to the symptom + propagated relevance over the merged
    code+state graph), softmax, take the top-n. This is the no-LLM alternative to the agent."""
    import numpy as np
    from vard import propagate as P
    rg = idx["rg"]
    sg = SLG.build_state_graph(idx, repo)
    nodes = CH.content_nodes(rg)
    chunks, keys, bm = CH.chunk_index(nodes, repo)
    lex = _mm(CH.lexical_scores(task, keys, bm))
    sem = _mm(CH.semantic_scores(task, nodes, keys, chunks, repo))
    content = {nid: 0.5 * lex.get(nid, 0) + 0.5 * sem.get(nid, 0) for nid in set(lex) | set(sem)}
    # merged code+state graph PPR, seeded by content (file level)
    fseed = {}
    for nid, s in content.items():
        f = rg.nodes[nid].file
        fseed[f] = max(fseed.get(f, 0.0), s)
    edges = (idx.get("import_edges") or []) + SLG.state_file_edges(idx, repo, sg)
    files = sorted({n.file for n in nodes})
    ppr = P.ppr_scores(edges, files, fseed)
    # score each candidate state type by its class node's content + its file's propagated relevance
    score = {}
    for t, dids in sg["type_def"].items():
        if SLG._INFRA.search(t):
            continue
        best = 0.0
        for did in dids:
            best = max(best, content.get(did, 0.0) + 0.6 * ppr.get(rg.nodes[did].file, 0.0))
        score[t] = best
    # softmax is monotonic in score, so top-n by score == top-n of the softmax distribution
    return sorted(score, key=score.get, reverse=True)[:topn]


def structural_identify(idx, task, repo, topn=12):
    """Fairer non-LLM identifier: do NOT score state by lexical/semantic similarity (known blind to
    dark state). Instead ANCHOR on the operations/entities the symptom NAMES (method/type names), then
    follow STRUCTURAL def-use: the state types referenced in those anchors' bodies (and 1 call hop).
    Rank by how many anchors reach each state type. This is the structural-feature attention."""
    rg = idx["rg"]
    sg = SLG.build_state_graph(idx, repo)
    idents, _ = C.extract_seeds(task)
    identlc = {i.lower() for i in idents}
    anchors = [n for n in rg.nodes.values()
               if n.type in ("function", "method", "class") and (n.name or "").lower() in identlc]
    # 1 call hop: include callees/callers of the anchors (def-use neighborhood of the named operation)
    anchor_ids = {a.id for a in anchors}
    for cs in getattr(rg, "call_sites", []):
        # call_sites entries vary; be defensive
        try:
            caller, callee = cs[0], cs[1]
        except Exception:
            continue
        if caller in anchor_ids or callee in anchor_ids:
            for x in (caller, callee):
                if x in rg.nodes:
                    anchor_ids.add(x)
    cache = {}
    score = {}
    for aid in anchor_ids:
        a = rg.nodes[aid]
        txt = SLG._node_text(repo, a, cache)
        for t in set(SLG._CAP.findall(txt)):
            if t in sg["type_def"] and not SLG._INFRA.search(t) and t != a.name:
                score[t] = score.get(t, 0) + 1
    return sorted(score, key=score.get, reverse=True)[:topn]


def save_identified(task, types):
    d = {}
    if os.path.isfile(IDENT_FILE):
        d = json.load(open(IDENT_FILE))
    d[_key(task)] = types
    os.makedirs(os.path.dirname(IDENT_FILE), exist_ok=True)
    json.dump(d, open(IDENT_FILE, "w"))


def load_identified(task):
    if not os.path.isfile(IDENT_FILE):
        return []
    return json.load(open(IDENT_FILE)).get(_key(task), [])


def agent_state_closure(idx, task, repo):
    sg = SLG.build_state_graph(idx, repo)
    rg = idx["rg"]
    closure = set()
    for t in load_identified(task):
        if t in sg["type_def"]:
            closure |= SLG._type_closure(sg, t, rg)
    return closure
