#!/usr/bin/env python3
"""Function-level coupling via TYPE-RESOLVED shared-field def-use, with noise filters.
funB writes Type.field, funA reads Type.field -> couple them (no call/import link). Receiver types come
from VARD's var_types (this -> enclosing class). Filters keep it precise:
  - drop JDK/primitive receiver types (String.x, List.x ... are misresolutions),
  - drop obvious base/abstract types (Base*/Abstract* + a small denylist) that collapse subclasses,
  - IDF cap: drop fields touched by too many functions (hub fields = noise).

  python -m eval.funccoupling report <repo>     # explosion/precision report on a repo
  python -m eval.funccoupling eval              # does it recover gold the current pool misses? (curated bugs)
"""
import sys, os, re, glob, collections
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from vard import cli, state as ST, rank as RK, selflabel as SL
from eval import dataset as D, metric as M, channels as CH, recall_union as RU

_JDK = {"String", "Integer", "Long", "Short", "Byte", "Boolean", "Double", "Float", "Character", "Object",
        "List", "ArrayList", "LinkedList", "Map", "HashMap", "LinkedHashMap", "TreeMap", "Set", "HashSet",
        "Collection", "Optional", "BigDecimal", "BigInteger", "Date", "LocalDate", "LocalDateTime", "Instant",
        "Calendar", "StringBuilder", "StringBuffer", "Class", "Void", "Number", "Iterable", "Stream", "Arrays",
        "Collections", "Objects", "Math", "System", "Thread", "Exception", "RuntimeException", "Throwable",
        "File", "Path", "URI", "URL", "Pattern", "Matcher", "UUID", "Duration", "Charset", "T", "E", "K", "V"}
_BASE_RX = re.compile(r'(^Base|^Abstract|^Super|Model$|Entity$|^Object$|DTO$|^Base)')   # collapse-prone bases
_HUB = 12                                                                                # IDF cap: drop hub fields

_RW = re.compile(r'\b(\w+)\.(\w+)\s*(?:[-+*/]?=)(?!=)')
_RR = re.compile(r'\b(\w+)\.(\w+)\b(?!\s*[=(])')


_KW = set(("if else for while do switch case return new public private protected static final void abstract "
           "class interface enum extends implements import package this super try catch finally throw throws "
           "int long short byte char float double boolean true false null synchronized volatile transient "
           "instanceof break continue default goto const var let function async await typeof in of").split())
_ID = re.compile(r'\b([A-Za-z_]\w*)\b')
_ASSIGN = re.compile(r'\b([A-Za-z_]\w*)\s*(?:[-+*/%&|^]?=)(?!=)')
_INCR = re.compile(r'([A-Za-z_]\w*)\s*(?:\+\+|--)|(?:\+\+|--)\s*([A-Za-z_]\w*)')


def typed_field_index(idx, repo, mode="filtered"):
    """{ key: (writer_nodeids, reader_nodeids) }.
    mode: 'filtered' = type-resolved + JDK/base/IDF filters; 'full' = type-resolved, NO filters;
          'name' = couple by bare field name; 'allvars' = EVERY identifier written<->read (maximal graph)."""
    rg = idx["rg"]; vt_all = getattr(rg, "var_types", {}); cache = {}
    w, r = collections.defaultdict(set), collections.defaultdict(set)

    if mode == "allvars":
        for n in ST._content_nodes(rg):
            if n.type == "class":
                continue
            txt = ST._node_text(repo, n, cache)
            writes = {m for m in _ASSIGN.findall(txt)} | {a or b for a, b in _INCR.findall(txt)}
            writes = {x for x in writes if x and x not in _KW and len(x) > 1}
            for k in writes:
                w[k].add(n.id)
            for k in {x for x in _ID.findall(txt) if x not in _KW and len(x) > 1} - writes:
                r[k].add(n.id)
        return {k: (w[k], r[k]) for k in w if r.get(k)}

    def keep(t):
        if mode == "full":
            return bool(t) and t[0:1].isupper()
        return t and t not in _JDK and not _BASE_RX.search(t) and t[0:1].isupper()

    for n in ST._content_nodes(rg):
        if n.type == "class":
            continue
        txt = ST._node_text(repo, n, cache)
        vt = vt_all.get(n.file, {})
        cls = n.qual.split("::")[-1].split(".")[0]
        wk = set()
        for recv, fld in _RW.findall(txt):
            if fld.isupper() or len(fld) < 2:
                continue
            if mode == "name":
                k = fld
            else:
                t = cls if recv == "this" else vt.get(recv)
                if not keep(t):
                    continue
                k = f"{t}.{fld}"
            wk.add(k); w[k].add(n.id)
        for recv, fld in _RR.findall(txt):
            if fld.isupper() or len(fld) < 2:
                continue
            if mode == "name":
                k = fld
            else:
                t = cls if recv == "this" else vt.get(recv)
                if not keep(t):
                    continue
                k = f"{t}.{fld}"
            if k not in wk:
                r[k].add(n.id)
    out = {}
    for k in w:
        if not r.get(k):
            continue
        if mode == "filtered" and len(w[k]) + len(r[k]) > _HUB:     # IDF cap only in filtered mode
            continue
        out[k] = (w[k], r[k])
    return out


def field_partners(idx, repo, seeds, fidx=None, mode="filtered"):
    """Nodes coupled to the seed nodes through a shared field (writer<->reader)."""
    fidx = fidx if fidx is not None else typed_field_index(idx, repo, mode)
    seeds = set(seeds)
    out = set()
    for k, (w, r) in fidx.items():
        if w & seeds:
            out |= r
        if r & seeds:
            out |= w
    return out - seeds


def report(repo):
    repo = os.path.abspath(os.path.expanduser(repo)); idx = cli.fresh_index(repo)
    fidx = typed_field_index(idx, repo)
    edges = sum(len(w) * len(r) for w, r in fidx.values())
    print(f"repo {os.path.basename(repo)}: {len(fidx)} clean coupled fields, {edges} edges")
    for k, (w, r) in sorted(fidx.items(), key=lambda kv: -len(kv[1][0]) * len(kv[1][1]))[:10]:
        print(f"   {k:34s} {len(w)}w x {len(r)}r")


def _load(source):
    if source == "cb":
        os.environ["VARD_CB_NOCLONE"] = "1"; os.environ.setdefault("VARD_CB_REPO_CAP", "2")
        from eval import contextbench as CB
        bugs = []
        for lang in ["java", "go", "javascript", "typescript"]:   # cloned, faster; skip python giants
            try:
                bugs += CB.load_cb_bugs(split="full", lang=lang)
            except Exception as e:
                print(f"  ! {lang}: {str(e)[:50]}")
        return bugs
    return D.load_bugs([p for p in glob.glob("eval/bugs/*.json") if not os.path.basename(p).startswith("_")])


def evaluate(mode="filtered", source="curated"):
    bugs = _load(source)
    rows = []
    for bug in bugs:
        try:
            idx = cli.fresh_index(bug.repo_dir); rg = idx["rg"]
            gold = M.gold_symbols(rg, bug.gold)
            if not gold:
                continue
            nodes = CH.content_nodes(rg)
            cs, _ = RK.rank_nodes(idx, bug.issue_text, bug.repo_dir, nodes, weights=SL.load_weights(bug.repo_dir))
            seeds = set(sorted(cs, key=cs.get, reverse=True)[:8])
            fp = field_partners(idx, bug.repo_dir, seeds, mode=mode)
            rec = lambda s: len(gold & s) / len(gold)
            rows.append((bug.id, len(gold), rec(seeds), rec(fp), len(fp), len((gold & fp) - seeds)))
            print(f"  {bug.id[-28:]:28s} gold={len(gold):2d} cf@8={rec(seeds):.2f} "
                  f"field[{mode}]={rec(fp):.2f}(|{len(fp)}|) uniqAdd={len((gold & fp) - seeds)}", flush=True)
        except Exception as e:
            print(f"  ! {bug.id[-16:]}: {str(e)[:50]}", flush=True)
    n = len(rows)
    if n:
        print(f"\n  [{source}/{mode}] n={n}  cf@8={sum(r[2] for r in rows)/n:.2f}  "
              f"field-recall={sum(r[3] for r in rows)/n:.2f}  avg|fp|={sum(r[4] for r in rows)/n:.0f}  "
              f"bugs where field uniquely adds gold={sum(1 for r in rows if r[5])}")


if __name__ == "__main__":
    a = sys.argv[1:]
    if a and a[0] == "size":                  # how big is the maximal graph?
        repo = os.path.abspath(os.path.expanduser(a[1]))
        idx = cli.fresh_index(repo)
        fidx = typed_field_index(idx, repo, "allvars")
        edges = sum(len(w) * len(r) for w, r in fidx.values())
        print(f"{os.path.basename(repo)}: {len(fidx):,} variables coupled, {edges:,} writer-x-reader edges")
        for k, (w, r) in sorted(fidx.items(), key=lambda kv: -len(kv[1][0]) * len(kv[1][1]))[:10]:
            print(f"   {k:24s} {len(w):4d}w x {len(r):4d}r = {len(w)*len(r):,}")
    elif a and a[0] == "eval":
        evaluate(mode=(a[1] if len(a) > 1 else "filtered"))
    elif a and a[0] == "evalcb":
        evaluate(mode=(a[1] if len(a) > 1 else "filtered"), source="cb")
    elif len(a) > 1 and a[0] == "report":
        report(a[1])
    else:
        print(__doc__)
