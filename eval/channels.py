"""The three conventional reachability channels — what an agent's own search approximates.

  lexical   (BM25 over method passages)        ~ grep / keyword search
  semantic  (embeddings over the same passages) ~ embedding search
  structural(import-graph reachability)         ~ follow-the-imports / call hops

These reuse VARD's OWN implementations (same tokenizer, same passages, same graph) so the
metric is honest: "dark" gold is gold these signals miss, not gold a strawman misses.
History and the data-coupling layer are deliberately NOT here — those are the extra signals
whose job is to recover the dark gold.
"""
import re
import numpy as np
from rank_bm25 import BM25Okapi
from vard import common as C, embed as E, propagate as P

_IDENT = re.compile(r"[A-Za-z_]\w{2,}")
_BODY_CAP = 600


def content_nodes(rg):
    return [n for n in rg.nodes.values() if n.type in ("function", "method", "class")]


def chunk_index(nodes, repo):
    """Passage index shared by the lexical and semantic channels (mirrors rank.py)."""
    chunks = E.node_chunk_texts(nodes, repo)
    keys, cdocs = [], []
    for n in nodes:
        qt = C.subtokens(n.qual)
        for t in chunks[n.id]:
            keys.append(n.id)
            cdocs.append(qt + [m.lower() for m in _IDENT.findall(t)][:_BODY_CAP])
    return chunks, keys, BM25Okapi(cdocs)


def _max_by_node(keys, vals):
    out = {}
    for i, nid in enumerate(keys):
        v = float(vals[i])
        if v > out.get(nid, -1e30):
            out[nid] = v
    return out


def lexical_scores(task, keys, bm):
    idents, _ = C.extract_seeds(task)
    q = [t for s in idents for t in C.subtokens(s)] + C.subtokens(task.splitlines()[0] if task else "")
    return _max_by_node(keys, bm.get_scores(q or ["x"]))


def semantic_scores(task, nodes, keys, chunks, repo):
    try:
        nv = E.embed_nodes(nodes, repo, "live")
        cvecs = np.vstack([nv[n.id][ci] for n in nodes for ci in range(len(chunks[n.id]))])
        return _max_by_node(keys, cvecs @ E.embed_task(task))
    except Exception as e:
        print(f"  ! semantic channel unavailable ({str(e)[:60]}) — treating as empty")
        return {}


def structural_reach_files(idx, seed_files, hops=1):
    """Files within `hops` import edges of the files an agent ALREADY surfaced by search.

    Faithful to how an agent uses imports: open a file you found, jump to the files it imports.
    The earlier version expanded a 2-hop closure from raw issue keywords — in a 1,998-file
    monorepo that swept 57% of the repo (and seeded on junk words like 'been'/'also'), making
    'structurally reachable' trivially true and killing the dark-gold signal. Seeding from the
    lexical/semantic hits keeps it bounded and honest."""
    adj = P.undirected_adj(idx.get("import_edges") or [])
    reach, frontier = set(seed_files), set(seed_files)
    for _ in range(hops):
        nxt = set()
        for f in frontier:
            nxt |= adj.get(f, set())
        nxt -= reach
        reach |= nxt
        frontier = nxt
    return reach


def topk_ids(scores, k):
    return set(sorted(scores, key=scores.get, reverse=True)[:k])
