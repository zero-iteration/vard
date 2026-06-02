#!/usr/bin/env python3
"""Repository Attention Graph — symbol-level nodes + typed edges, multi-language.

Built via the language providers (see vard/languages/): each file yields uniform
symbols + call-sites; this assembles them into a RepoGraph with typed edges
(contains / inherits) and attaches call-sites / decorators / declared types for the
data-resource layer. No source code is stored on nodes — only navigational structure.
"""
import os
from collections import defaultdict
import networkx as nx

CODE_SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "build", "dist",
                  "__pycache__", ".tox", ".mypy_cache", "site-packages"}


class Node:
    __slots__ = ("id", "type", "file", "start", "end", "name", "qual")
    def __init__(self, nid, ntype, file, start, end, name, qual):
        self.id, self.type, self.file = nid, ntype, file
        self.start, self.end, self.name, self.qual = start, end, name, qual


class RepoGraph:
    def __init__(self, repo_dir):
        self.repo_dir = repo_dir
        self.G = nx.MultiDiGraph()          # typed edges via key=edge_type
        self.nodes = {}                      # id -> Node
        self.by_file = defaultdict(list)     # rel_file -> [Node]
        self.symtab = {}                     # qualified_name -> node id

    def _add(self, nid, ntype, file, start, end, name, qual):
        if nid in self.nodes:
            return self.nodes[nid]
        n = Node(nid, ntype, file, start, end, name, qual)
        self.nodes[nid] = n
        self.by_file[file].append(n)
        self.G.add_node(nid, type=ntype)
        self.symtab.setdefault(qual, nid)
        return n

    def stats(self):
        nt, et = defaultdict(int), defaultdict(int)
        for n in self.nodes.values():
            nt[n.type] += 1
        for _, _, k in self.G.edges(keys=True):
            et[k] += 1
        return {"nodes": dict(nt), "edges": dict(et),
                "n_nodes": len(self.nodes), "n_edges": self.G.number_of_edges()}

    def nodes_for_span(self, file, start, end):
        """Graph nodes whose line range overlaps [start,end] in `file` (smallest first)."""
        out = [n for n in self.by_file.get(file, []) if n.start <= end and n.end >= start]
        out.sort(key=lambda n: n.end - n.start)
        return out


def build_graph(repo_dir, extra_roots=None):
    """Multi-language graph via language providers (python/java/js/ts/go). Produces a
    RepoGraph + rg.call_sites + rg.node_decorators + rg.var_types for the resource layer.

    extra_roots: additional source dirs (e.g. dependency modules outside the repo tree). Their files
    are stored with paths relative to repo_dir (may be `../...`), so everything that joins repo_dir +
    rel still resolves — letting one graph span the whole multi-module project + source deps."""
    from . import languages as L
    rg = RepoGraph(repo_dir)
    rg.call_sites = []
    rg.node_decorators = {}
    rg.var_types = {}                          # file -> {var/field name: declared type}
    name_index = defaultdict(list)            # class simple-name -> [node ids] for inherits
    exts = L.supported_extensions()
    files = []
    seen_abs = set()
    for base in [repo_dir] + list(extra_roots or []):
        for root, dirs, fs in os.walk(base):
            dirs[:] = [d for d in dirs if d not in CODE_SKIP_DIRS]
            for f in fs:
                if os.path.splitext(f)[1].lower() in exts:
                    ab = os.path.join(root, f)
                    if ab in seen_abs:
                        continue
                    seen_abs.add(ab)
                    files.append(os.path.relpath(ab, repo_dir))
    rg.skipped = 0
    for rel in files:
        prov = L.provider_for(rel)
        if not prov:
            continue
        if rel.endswith((".min.js", ".min.ts", ".bundle.js")) or any(
                seg in ("vendor", "bower_components", "node_modules", "third_party") for seg in rel.split(os.sep)):
            continue
        try:
            src = open(os.path.join(repo_dir, rel), encoding="utf-8", errors="ignore").read()
            if src and max((len(l) for l in src.splitlines()), default=0) > 2000:
                continue                              # minified / generated — skip
            art = prov.parse(repo_dir, rel, src)
            for sym in art.symbols:
                nid = f"{rel}::{sym.qual}"
                rg._add(nid, sym.type, rel, sym.start, sym.end, sym.name, sym.qual)
                if sym.type == "class":
                    rg.G.nodes[nid]["_bases"] = sym.bases
                    name_index[sym.name].append(nid)
                if sym.decorators:
                    rg.node_decorators[nid] = sym.decorators
            for sym in art.symbols:
                if sym.type == "module":
                    continue
                pid = f"{rel}::{sym.parent_qual}" if sym.parent_qual else f"{rel}::<module>"
                cid = f"{rel}::{sym.qual}"
                if pid not in rg.nodes:
                    pid = f"{rel}::<module>"
                if pid in rg.nodes and cid in rg.nodes:
                    rg.G.add_edge(pid, cid, key="contains")
            rg.call_sites.extend(art.calls)
            if art.var_types:
                rg.var_types[rel] = art.var_types
        except Exception:
            rg.skipped += 1                           # one malformed file must not crash the whole index
            continue
    for nid, data in list(rg.G.nodes(data=True)):
        for b in data.get("_bases", []):
            for tgt in name_index.get(b, []):
                if tgt != nid:
                    rg.G.add_edge(nid, tgt, key="inherits")
    return rg
