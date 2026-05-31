"""Structural propagation: a file-level import graph + personalized PageRank.

In benchmarks a learned reranker weighted this signal +0.47 — its 3rd-strongest feature, ahead of
HyDE (only content and commit-history scored higher). Intuition: a file depended on by many code
regions relevant to the task is itself likely relevant, even if its own text doesn't match. The
graph is static per repo snapshot (built once at index time); only the personalized PageRank runs
per query, seeded by the task's content scores. Multi-language: Python, JS/TS, Java (FQN imports)."""
import os
import re

# from <dots><module> import <names>   |   import <module>
PY_IMP = re.compile(r"^[ \t]*(?:from[ \t]+(\.*)([\w.]*)[ \t]+import[ \t]+([\w*, \t]+)|import[ \t]+([\w.]+))", re.M)
JS_IMP = re.compile(r"""(?:from|require\()[ \t]*['"]([^'"]+)['"]""")
JAVA_PKG = re.compile(r"^[ \t]*package[ \t]+([\w.]+)[ \t]*;", re.M)
JAVA_IMP = re.compile(r"^[ \t]*import[ \t]+(?:static[ \t]+)?([\w.]+(?:\.\*)?)[ \t]*;", re.M)
_JS_EXT = ("", ".js", ".ts", ".jsx", ".tsx", "/index.js", "/index.ts", "/index.jsx", "/index.tsx")


def _py_targets(f, dots, module, names, mod2file, fileset):
    """Resolve a Python import to repo file(s). Handles absolute (a.b.c) and relative
    (from . import x / from ..pkg import y) forms."""
    out = []
    if dots:                                            # relative import
        cur = os.path.dirname(f)
        for _ in range(len(dots) - 1):
            cur = os.path.dirname(cur)
        bases = ["/".join([cur] + module.split(".")).strip("/")] if module else \
                ["/".join([cur, nm.strip().split()[0]]).strip("/") for nm in names.split(",") if nm.strip() and nm.strip() != "*"]
        for b in bases:
            for cand in (b + ".py", b + "/__init__.py"):
                if cand in fileset:
                    out.append(cand)
    else:                                               # absolute dotted module
        parts = module.split("."); tgt = None
        while parts and tgt is None:
            tgt = mod2file.get(".".join(parts)); parts = parts[:-1]
        if tgt:
            out.append(tgt)
    return out


def _read(repo, rel, n=6000):
    try:
        return open(os.path.join(repo, rel), encoding="utf-8", errors="ignore").read()[:n]
    except Exception:
        return ""


def build_import_edges(repo, files):
    """Static file→file import edges (computed once at index time). f→g means f imports g.
    Resolves Python dotted/relative imports, JS/TS relative imports, and Java FQN imports."""
    fileset = set(files)
    texts = {f: _read(repo, f) for f in files}
    mod2file = {}                                   # python dotted module -> file
    fqn2file = {}; pkg2files = {}                   # java FQN -> file, package -> [files]
    for f in files:
        if f.endswith(".py"):
            base = f[:-3].replace(os.sep, "/")
            mod2file.setdefault(base.replace("/", "."), f)
            if base.endswith("/__init__"):
                mod2file.setdefault(base[:-9].replace("/", "."), f)
        elif f.endswith(".java") and texts[f]:
            m = JAVA_PKG.search(texts[f]); pkg = m.group(1) if m else ""
            fqn = (pkg + "." if pkg else "") + os.path.basename(f)[:-5]
            fqn2file.setdefault(fqn, f); pkg2files.setdefault(pkg, []).append(f)
    edges = []
    for f in files:
        txt = texts[f]
        if not txt:
            continue
        if f.endswith(".py"):
            fn = f.replace(os.sep, "/")
            for m in PY_IMP.finditer(txt):
                dots, module, names, plain = m.group(1), m.group(2) or "", m.group(3) or "", m.group(4)
                if plain is not None:
                    dots, module = "", plain
                for tgt in _py_targets(fn, dots, module, names, mod2file, fileset):
                    if tgt != fn:
                        edges.append((fn, tgt))
        elif f.endswith((".js", ".ts", ".jsx", ".tsx")):
            d = os.path.dirname(f)
            for m in JS_IMP.finditer(txt):
                spec = m.group(1)
                if not spec.startswith("."):
                    continue
                cand = os.path.normpath(os.path.join(d, spec)).replace(os.sep, "/")
                for ext in _JS_EXT:
                    if cand + ext in fileset:
                        edges.append((f, cand + ext)); break
        elif f.endswith(".java"):
            for m in JAVA_IMP.finditer(txt):
                imp = m.group(1)
                if imp.endswith(".*"):                       # wildcard: link to whole package
                    for tgt in pkg2files.get(imp[:-2], ()):
                        if tgt != f:
                            edges.append((f, tgt))
                else:                                        # FQN (drop trailing segments for static/nested)
                    parts = imp.split("."); tgt = None
                    while parts and tgt is None:
                        tgt = fqn2file.get(".".join(parts)); parts = parts[:-1]
                    if tgt and tgt != f:
                        edges.append((f, tgt))
    return edges


def undirected_adj(edges):
    """file -> set(neighbor files), import edges treated as undirected reachability."""
    adj = {}
    for a, b in edges:
        adj.setdefault(a, set()).add(b)
        adj.setdefault(b, set()).add(a)
    return adj


def ppr_scores(edges, files, seed, alpha=0.5, top_seeds=15):
    """Personalized PageRank over the import graph, scored as LIFT over the uniform-popularity
    baseline (personalized ÷ baseline).

    Two design choices, both validated on a real Spring Boot repo:
    - SPARSE seeding: restart only from the top-`top_seeds` content candidates, NOT from every file.
      Dense seeding (propagate from all files) let the long tail vote and generic high-fan-in files
      (entities/DTOs/MyBatis Example classes) accumulate mass from everyone — popularity bias. Seeding
      only the strong candidates lights up *their* neighborhoods instead. (top_seeds=None → dense.)
    - LIFT over baseline: a file that ranks high only because it's imported everywhere cancels out;
      one reached specifically because task-relevant code depends on it stands out.
    Returns {file: score} in [0,1]; zeros if no graph."""
    if not edges:
        return {f: 0.0 for f in files}
    try:
        import networkx as nx
        G = nx.DiGraph(); G.add_nodes_from(files); G.add_edges_from(edges)
        if top_seeds:
            keep = set(sorted(seed, key=lambda f: seed.get(f, 0.0), reverse=True)[:top_seeds])
            pers = {f: (max(float(seed.get(f, 0.0)), 0.0) if f in keep else 0.0) + 1e-9 for f in files}
        else:
            pers = {f: max(float(seed.get(f, 0.0)), 0.0) + 1e-9 for f in files}
        pr = nx.pagerank(G, alpha=alpha, personalization=pers, max_iter=80)
        base = nx.pagerank(G, alpha=alpha, max_iter=80)        # uniform restart = popularity prior
    except Exception:
        return {f: 0.0 for f in files}
    lift = {f: pr[f] / (base[f] + 1e-12) for f in pr}
    lo, hi = min(lift.values()), max(lift.values())
    return {f: (v - lo) / (hi - lo) if hi > lo else 0.0 for f, v in lift.items()}
