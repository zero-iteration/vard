"""Self-labeling loop (offline): learn this repo's ranking weights from its own git history.

Each past commit is a free labeled example — the message is a pseudo-query, the code files it touched
are the positives. We compute the same features the ranker uses (content, commit-history, graph-PPR)
for a candidate pool per commit and fit a logistic ranker, then express the learned auxiliary
importances *relative to the content backbone* and persist them. `vard learn` runs this; the ranker
loads the result if present, else falls back to the benchmark-tuned defaults. This is the flywheel's
offline half (no live exploration). Caveat: features are computed on the current snapshot, so there is
mild look-ahead leakage — acceptable for learning relative signal weights, not for absolute recall."""
import json
import os

WEIGHTS_FILE = "weights.json"


def _mm(a):
    import numpy as np
    a = np.asarray(a, float)
    return (a - a.min()) / (a.max() - a.min() + 1e-9)


def weights_path(repo):
    return os.path.join(os.path.abspath(repo), ".vard", WEIGHTS_FILE)


def load_weights(repo):
    try:
        with open(weights_path(repo)) as _f:
            w = json.load(_f)
        return {k: v for k, v in w.items() if not k.startswith("_")}
    except Exception:
        return None


def train_weights(repo, idx, sample=150, pool=50, cap=1.5):
    """Fit per-repo weights from commit history. Returns a weights dict (subset of the ranker's
    keys, relative to content≙1.0) or None if there isn't enough signal."""
    import numpy as np
    from collections import defaultdict
    try:
        from sklearn.linear_model import LogisticRegression
        from sklearn.preprocessing import StandardScaler
        from rank_bm25 import BM25Okapi
    except Exception:
        return None
    from . import common as C, history as H, propagate as P
    try:
        from . import embed as E
        have_emb = os.environ.get("VARD_EMB_MODEL", "x") != "none"
    except Exception:
        have_emb = False
    rg = idx["rg"]; commits = idx.get("history") or H.mine(repo)
    if not commits:
        return None
    files = sorted({n.file for n in rg.nodes.values()})
    if len(files) < 5:
        return None
    fidx = {f: i for i, f in enumerate(files)}
    file_nodes = defaultdict(list)
    for n in rg.nodes.values():
        file_nodes[n.file].append(n)
    docs = [C.subtokens(f) + [t for nn in file_nodes[f] for t in C.subtokens(nn.qual)] for f in files]
    bm = BM25Okapi(docs)
    demb = E.embed_texts([" ".join(d)[:500] for d in docs]) if have_emb else None
    edges = idx.get("import_edges") or []
    use = [c for c in commits if 1 <= len(set(c["files"]) & set(files)) <= 8][:sample]
    if len(use) < 10:
        return None
    X, y = [], []
    for c in use:
        touched = set(c["files"]) & set(files)
        msg = c["msg"]
        q = [t for s in C.extract_seeds(msg)[0] for t in C.subtokens(s)] + C.subtokens(msg.splitlines()[0] if msg else "")
        cbm = _mm(bm.get_scores(q or ["x"]))
        csem = _mm(demb @ E.embed_task(msg)) if demb is not None else np.zeros(len(files))
        cs = 0.5 * csem + 0.5 * cbm if demb is not None else cbm
        hsc = H.candidate_files([cc for cc in commits if cc is not c], msg, k=300)
        hs = np.array([hsc.get(f, 0.0) for f in files]); hs = _mm(hs) if hs.max() > 0 else hs
        pr = P.ppr_scores(edges, files, {f: float(cs[fidx[f]]) for f in files})
        ppr = np.array([pr.get(f, 0.0) for f in files])
        cand = set(np.argsort(cs)[::-1][:pool].tolist()) | {fidx[f] for f in touched}
        for j in cand:
            X.append([cs[j], hs[j], ppr[j]]); y.append(1 if files[j] in touched else 0)
    if sum(y) < 5 or len(set(y)) < 2:
        return None
    sc = StandardScaler().fit(X)
    clf = LogisticRegression(max_iter=1000, class_weight="balanced").fit(sc.transform(X), y)
    co = clf.coef_[0]                                   # [content, history, ppr] on standardized feats
    if co[0] <= 1e-6:
        return None
    # importances relative to the content backbone, clamped
    raw = {"history": min(max(co[1] / co[0], 0.0), cap), "ppr": min(max(co[2] / co[0], 0.0), cap)}
    # SHRINK toward the benchmark-tuned prior: per-repo history mining is small and biased (the
    # commit-message labels share vocabulary with the message-derived history feature, inflating it),
    # so learning should *adjust* the defaults, not replace them. shrink=0.5 = equal trust.
    from .rank import DEFAULT_WEIGHTS as D
    shrink = 0.5
    w = {k: float(shrink * D[k] + (1 - shrink) * raw[k]) for k in raw}
    w["_meta"] = {"n_commits": len(use), "n_examples": len(y), "n_pos": int(sum(y)),
                  "raw_relative": {k: float(v) for k, v in raw.items()},
                  "raw_coefs": {"content": float(co[0]), "history": float(co[1]), "ppr": float(co[2])}}
    return w


def learn(repo, idx, sample=150):
    """Train and persist per-repo weights. Returns the saved dict or None."""
    w = train_weights(repo, idx, sample=sample)
    if not w:
        return None
    os.makedirs(os.path.join(os.path.abspath(repo), ".vard"), exist_ok=True)
    with open(weights_path(repo), "w") as _o:
        json.dump(w, _o, indent=2)
    return w
