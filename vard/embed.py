#!/usr/bin/env python3
"""Embedding layer with disk cache. Backends: OpenAI (SOTA) or sentence-transformers.

Two-tier: file embeddings (recall) + node embeddings (precision), both cached to disk.
"""
import os, re, hashlib, time, sys
import numpy as np

KEY_FILE = os.path.expanduser("~/.config/vard/openai.key")


def _cache_dir(repo_dir):
    # VARD_EMB_CACHE_DIR lets the per-repo embedding cache live outside the working tree (e.g. a
    # persistent/shared location), so content-hashed method embeddings survive across checkouts and
    # only changed methods re-embed. Defaults to <repo>/.vard/cache/emb.
    base = os.environ.get("VARD_EMB_CACHE_DIR")
    d = base if base else os.path.join(repo_dir, ".vard", "cache", "emb")
    os.makedirs(d, exist_ok=True)
    return d
# Default to a LOCAL model (no API key). Set VARD_EMB_MODEL=openai:text-embedding-3-large
# for the SOTA cloud embedder, or VARD_EMB_MODEL=none to run embedding-free (BM25 + graph only).
_MODEL_NAME = os.environ.get("VARD_EMB_MODEL", "BAAI/bge-small-en-v1.5")
_CLIENT = None
_ST = None


def _openai_client():
    global _CLIENT
    if _CLIENT is None:
        from openai import OpenAI
        key = os.environ.get("OPENAI_API_KEY")
        if not key and os.path.isfile(KEY_FILE):
            key = open(KEY_FILE).read().strip()
        _CLIENT = OpenAI(api_key=key)
    return _CLIENT


def _is_openai():
    return _MODEL_NAME.startswith("openai:")


def _embed_openai(texts, batch=256):
    client = _openai_client()
    model = _MODEL_NAME.split(":", 1)[1]
    out = []
    for i in range(0, len(texts), batch):
        chunk = [(t or " ")[:8000] for t in texts[i:i + batch]]
        for attempt in range(6):
            try:
                resp = client.embeddings.create(model=model, input=chunk)
                out.extend([d.embedding for d in resp.data]); break
            except Exception as e:
                if attempt == 5: raise
                time.sleep(2 ** attempt)
    v = np.asarray(out, dtype=np.float32)
    v /= (np.linalg.norm(v, axis=1, keepdims=True) + 1e-9)
    return v


def _device():
    """Auto-detect the best available device. Hardcoding 'mps' broke every non-Apple-Silicon machine
    (the embedder threw, callers silently fell back to BM25-only). Try cuda -> mps -> cpu."""
    try:
        import torch
        if torch.cuda.is_available():
            return "cuda"
        if getattr(torch.backends, "mps", None) is not None and torch.backends.mps.is_available():
            return "mps"
    except Exception:
        pass
    return "cpu"


def _embed_st(texts, batch=64):
    global _ST
    if _ST is None:
        from sentence_transformers import SentenceTransformer
        kw = {"device": _device()}
        if "jina" in _MODEL_NAME or "nomic" in _MODEL_NAME: kw["trust_remote_code"] = True
        _ST = SentenceTransformer(_MODEL_NAME, **kw)
    return np.asarray(_ST.encode(texts, batch_size=batch, normalize_embeddings=True,
                                 show_progress_bar=False), dtype=np.float32)


def embeddings_disabled():
    return _MODEL_NAME.strip().lower() in ("none", "off", "")


def embed_texts(texts):
    # VARD_EMB_MODEL=none means embedding-free (BM25 + graph only). Fail fast and LOCALLY — never construct
    # SentenceTransformer("none"), which would fire a (failing) HuggingFace network call. Callers that don't
    # pre-check the env (e.g. memory.recall's fallback) catch this and degrade cleanly.
    if embeddings_disabled():
        raise RuntimeError("embeddings disabled (VARD_EMB_MODEL=none) — BM25/graph only")
    if not texts:
        return np.zeros((0, 8), dtype=np.float32)
    return _embed_openai(texts) if _is_openai() else _embed_st(texts)


def _tag():
    return re.sub(r"[^A-Za-z0-9]+", "_", _MODEL_NAME)


def _repo_tag(repo_dir):
    return re.sub(r"[^A-Za-z0-9]+", "_", repo_dir.split("/")[-2])


def embed_files(rg, repo_dir, commit):
    path = os.path.join(_cache_dir(repo_dir), f"files__{commit[:12]}__{_tag()}.npz")
    files = sorted(rg.by_file.keys())
    if os.path.isfile(path):
        d = np.load(path, allow_pickle=True)
        if list(d["ids"]) == files:
            v = d["vecs"]; return {files[i]: v[i] for i in range(len(files))}
    docs = [(f + " " + " ".join(n.qual.split(".")[-1] for n in rg.by_file[f][:60]))[:1500] for f in files]
    vecs = embed_texts(docs)
    np.savez_compressed(path, ids=np.array(files, dtype=object), vecs=vecs)
    return {files[i]: vecs[i] for i in range(len(files))}


def _chunks(text, size=1400, overlap=200):
    """Split a method's full source into ~model-window-sized passages (overlapping). Long methods
    overflow the embedder's context AND dilute a small concern into one averaged vector — chunking
    + best-passage scoring (max over chunks, done at query time) fixes both."""
    text = text or " "
    if len(text) <= size:
        return [text]
    out, i = [], 0
    while i < len(text):
        out.append(text[i:i + size])
        if i + size >= len(text):
            break
        i += size - overlap
    return out


def node_chunk_texts(node_list, repo_dir):
    """Split each node's FULL source into passages — the shared retrieval unit. Both the lexical
    (BM25) and semantic (embedding) halves index these SAME pieces, so a method is scored by its
    single best-matching passage rather than as one diluted whole."""
    filecache = {}
    def src(n):
        if n.file not in filecache:
            try: filecache[n.file] = open(os.path.join(repo_dir, n.file), encoding="utf-8", errors="ignore").read().splitlines()
            except Exception: filecache[n.file] = []
        return f"{n.type} {n.qual}\n" + "\n".join(filecache[n.file][n.start - 1:n.end])
    return {n.id: _chunks(src(n)) for n in node_list}


def embed_nodes(node_list, repo_dir, commit=None):
    """Embed each node by its FULL source, split into passages. Returns {id: ndarray (n_chunks, d)};
    callers score a node by its best-matching passage (max over chunks). Persistent per-repo cache
    keyed by per-chunk CONTENT HASH, so only changed code is re-embedded on refresh."""
    import hashlib
    path = os.path.join(_cache_dir(repo_dir), f"nodesC__{_tag()}.npz")
    cache = {}
    if os.path.isfile(path):
        d = np.load(path, allow_pickle=True)
        ids = list(d["ids"]); v = d["vecs"]
        cache = {ids[i]: v[i] for i in range(len(ids))}
    node_chunks = node_chunk_texts(node_list, repo_dir)
    chunk_hash = {n.id: [hashlib.md5(c.encode()).hexdigest() for c in node_chunks[n.id]] for n in node_list}
    missing = {}                                          # hash -> text (dedup across nodes)
    for n in node_list:
        for h, c in zip(chunk_hash[n.id], node_chunks[n.id]):
            if h not in cache and h not in missing:
                missing[h] = c
    if missing:
        if len(missing) > 200:                            # first run / big change: this is the slow part
            extra = "" if _is_openai() else f" on {_device()} (first run downloads the model ~90MB)"
            print(f"→ vard: embedding {len(missing)} code passages{extra}...", file=sys.stderr, flush=True)
        hs = list(missing); vecs = embed_texts([missing[h] for h in hs])
        for h, vec in zip(hs, vecs): cache[h] = vec
        ids = list(cache.keys())
        np.savez_compressed(path, ids=np.array(ids, dtype=object),
                            vecs=np.asarray([cache[i] for i in ids], dtype=np.float32))
    return {n.id: np.asarray([cache[h] for h in chunk_hash[n.id]], dtype=np.float32) for n in node_list}


def embed_task(problem_statement):
    from . import common as C
    return embed_texts([C.task_text(problem_statement)])[0]


def cos(a, b):
    return float(np.dot(a, b))
