#!/usr/bin/env python3
"""Persistent + self-updating index. Unchanged repo -> instant load. Changed repo ->
re-index (graph rebuild is cheap seconds; embeddings update only for changed code via
content-hash cache). Fingerprint = per-file (mtime,size) + git HEAD."""
import os, subprocess

SKIP = {".git", "node_modules", ".venv", "venv", "__pycache__", "build", "dist", ".vard", ".tox"}
# must cover every language build_graph indexes, else non-Python repos never detect changes → stale index
CODE_EXTS = (".py", ".pyi", ".java", ".js", ".jsx", ".ts", ".tsx", ".go", ".mjs", ".cjs")


def fingerprint(repo):
    repo = os.path.abspath(repo); fp = {}
    for root, dirs, files in os.walk(repo):
        dirs[:] = [d for d in dirs if d not in SKIP]
        for f in files:
            if f.endswith(CODE_EXTS):
                p = os.path.join(root, f)
                try:
                    st = os.stat(p); fp[os.path.relpath(p, repo)] = (int(st.st_mtime), st.st_size)
                except OSError:
                    pass
    head = ""
    try:
        head = subprocess.run(["git", "-C", repo, "rev-parse", "HEAD"],
                              capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception:
        pass
    return {"files": fp, "head": head}


def diff(old, new):
    o, n = old.get("files", {}), new.get("files", {})
    changed = [f for f in n if o.get(f) != n[f]]
    deleted = [f for f in o if f not in n]
    return changed, deleted


def is_fresh(old, new):
    if old.get("head") and new.get("head") and old["head"] != new["head"]:
        return False
    c, d = diff(old, new)
    return not c and not d
