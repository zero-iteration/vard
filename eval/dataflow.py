#!/usr/bin/env python3
"""Intra-procedural def-use slicing (ARISE-style), measured on the curated bugs' gold LINES.

ARISE's claim: data-flow slicing lifts function/LINE-level localization — the bottleneck. VARD returns
whole-method spans; the question is whether a def-use slice (from the query-relevant statement, forward
+ backward over def-use within the method) KEEPS the gold lines while DROPPING the rest — i.e. higher
line precision / fewer tokens at the same recall. Lightweight line-based def-use (no points-to). New
module; nothing in vard/ touched.

  python -m eval.dataflow            # all curated bugs
"""
import sys, os, re, glob, collections
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from vard import cli, common as C
from eval import dataset as D

_ASSIGN = re.compile(r'\b([A-Za-z_]\w*)\s*=(?!=)')                      # x = ...
_DECL = re.compile(r'\b[A-Z][\w<>,\[\].]*\s+([a-z_]\w*)\s*[=;)]')        # Type x ; / = / )
_IDENT = re.compile(r'\b([A-Za-z_]\w*)\b')
_KW = {"if", "for", "while", "return", "new", "this", "null", "true", "false", "else", "try",
       "catch", "throw", "public", "private", "protected", "static", "final", "void", "int",
       "long", "String", "boolean", "class", "import", "package", "switch", "case", "break"}


def defuse_slice(lines, seed):
    defs, uses = [], []
    for ln in lines:
        d = (set(_ASSIGN.findall(ln)) | set(_DECL.findall(ln))) - _KW
        u = (set(_IDENT.findall(ln)) - d) - _KW
        defs.append(d); uses.append(u)
    var_def, var_use = collections.defaultdict(set), collections.defaultdict(set)
    for i in range(len(lines)):
        for v in defs[i]:
            var_def[v].add(i)
        for v in uses[i]:
            var_use[v].add(i)
    sl = set(seed)
    changed = True
    while changed:
        changed = False
        for i in list(sl):
            for v in uses[i]:                                  # backward: defs feeding this line
                for j in var_def[v]:
                    if j <= i and j not in sl:
                        sl.add(j); changed = True
            for v in defs[i]:                                  # forward: uses of what this line defines
                for v_use in var_use[v]:
                    if v_use >= i and v_use not in sl:
                        sl.add(v_use); changed = True
    return sl


def _read(repo, rel):
    try:
        return open(os.path.join(repo, rel), encoding="utf-8", errors="ignore").read().splitlines()
    except Exception:
        return []


def main():
    paths = [p for p in glob.glob("eval/bugs/*.json") if not os.path.basename(p).startswith("_")]
    bugs = D.load_bugs(paths)
    agg = collections.defaultdict(list)
    n_meth = 0
    for bug in bugs:
        idx = cli.fresh_index(bug.repo_dir)
        rg = idx["rg"]
        idents = {x.lower() for x in C.extract_seeds(bug.issue_text)[0]}
        for span in bug.gold:
            cands = [n for n in rg.nodes_for_span(span.file, span.start, span.end)
                     if n.type in ("function", "method")]
            if not cands:
                continue                                       # class/field-level gold: not a within-method case
            m = min(cands, key=lambda n: n.end - n.start)
            lines = _read(bug.repo_dir, m.file)[m.start - 1:m.end]
            if len(lines) < 4:
                continue
            gold_local = {i for i in range(len(lines)) if span.start <= m.start + i <= span.end}
            if not gold_local:
                continue
            seed = {i for i, ln in enumerate(lines) if idents & {t.lower() for t in _IDENT.findall(ln)}}
            if not seed:
                seed = {0}                                     # fall back to the signature line
            sl = defuse_slice(lines, seed)
            n_meth += 1
            bucket = "large(>=30)" if len(lines) >= 30 else "small(<30)"
            for b in ("all", bucket):
                agg[(b, "size")].append(len(lines))
                agg[(b, "frac")].append(len(sl) / len(lines))
                agg[(b, "rec")].append(len(gold_local & sl) / len(gold_local))
    print(f"within-method gold sites measured: {n_meth}\n")
    print(f"  {'bucket':12s} {'n':>3s} {'avg lines':>9s} {'slice gold-recall':>18s} {'slice size':>11s}")
    for b in ("all", "small(<30)", "large(>=30)"):
        n = len(agg[(b, "size")])
        if not n:
            continue
        sz = sum(agg[(b, "size")]) / n
        rec = sum(agg[(b, "rec")]) / n
        frac = sum(agg[(b, "frac")]) / n
        print(f"  {b:12s} {n:>3d} {sz:9.1f} {rec:18.2f} {frac*100:10.0f}%")
    print("\n  (whole-method is recall 1.00 at 100% size by construction; slicing wins only if it keeps"
          " recall ~1.0 while cutting size — i.e. on LARGE methods.)")


if __name__ == "__main__":
    main()
