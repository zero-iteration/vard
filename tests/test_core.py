#!/usr/bin/env python3
"""Core regression suite for VARD's runtime/memory/rank/config logic — stdlib unittest, no JVM, no network.

Run:  VARD_EMB_MODEL=none python -m unittest discover -s tests -v
These codify the invariants the manual smoke tests were checking by hand: env-provenance accretion,
freshness staleness, typed-memory coexistence, the never-demote ranking invariant, observed-live config
values, and that `explain` surfaces the right divergences. Traces are synthesized in the agent's JSONL
format (so the JVM isn't needed); resolution/freshness/joins are exercised for real against a built index.
"""
import json
import os
import tempfile
import unittest
import warnings

os.environ.setdefault("VARD_EMB_MODEL", "none")          # lexical-only: deterministic, no model download
# VARD uses the `open(path).read()` idiom widely; under CPython the fd closes promptly on refcount-0, so the
# ResourceWarning is noise here (not an fd leak). Silence it so the suite signal stays clean.
warnings.simplefilter("ignore", ResourceWarning)

from vard import cli, runtime as RT, memory as MEM, rank as RK


def _write(root, rel, text):
    p = os.path.join(root, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as f:
        f.write(text)


SVC = '''\
class Service:
    def handle(self, n):
        return self.compute(n)

    def compute(self, n):
        return n * 2

    def refund(self, n):       # never "executed" in the synthetic traces
        return n - 1


class Conf:
    def get(self, key):
        return "OVERRIDE"
'''


def _mk_repo():
    """A tiny indexed repo + a few helpers to synthesize traces. Returns (root, idx-loader)."""
    root = tempfile.mkdtemp(prefix="vardtest_")
    _write(root, "pkg/svc.py", SVC)
    _write(root, "application.properties", "app.mode=FileVal\n")
    cli.build_index(root)
    return root


def _trace(root, records):
    """Write a synthetic agent trace file under .vard/trace/ and return its path."""
    d = os.path.join(root, ".vard", "trace")
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, "run.jsonl.1")
    with open(p, "w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")
    return p


class RuntimeIngestTest(unittest.TestCase):
    def setUp(self):
        self.root = _mk_repo()

    def _idx(self):
        idx = cli.load_index(self.root)
        RT.attach(idx, self.root)
        return idx

    def test_env_provenance_and_accretion(self):
        idx = cli.load_index(self.root)
        tp = _trace(self.root, [
            {"t": "config", "profile": "", "mode": "instrument", "env": "test"},
            {"t": "method", "qual": "Service.handle", "hits": 3},
            {"t": "edge", "caller": "Service.handle", "callee": "Service.compute", "n": 2},
        ])
        r1 = RT.ingest(idx, self.root, tp, env="test")
        self.assertTrue(r1["ok"])
        self.assertEqual(r1["env"], "test")
        # second run under a different env merges WITHOUT conflating provenance
        tp2 = _trace(self.root, [
            {"t": "config", "mode": "instrument", "env": "prod"},
            {"t": "method", "qual": "Service.handle", "hits": 5},
        ])
        RT.ingest(idx, self.root, tp2, env="prod")
        data = RT.load(self.root)
        self.assertEqual(set(data["runs"]), {"test", "prod"})
        handle = next(m for m in data["methods"] if m["qual"] == "Service.handle")
        self.assertEqual(set(handle["envs"]), {"test", "prod"})    # seen under BOTH, kept distinct
        self.assertEqual(handle["envs"]["test"] + handle["envs"]["prod"], handle["hits"])

    def test_malformed_line_does_not_drop_trace(self):
        idx = cli.load_index(self.root)
        d = os.path.join(self.root, ".vard", "trace"); os.makedirs(d, exist_ok=True)
        p = os.path.join(d, "run.jsonl.9")
        with open(p, "w") as f:
            f.write('{"t":"config","env":"test"}\n')
            f.write('this is not json — must be skipped, not fatal\n')
            f.write('{"t":"method","qual":"Service.compute","hits":1}\n')
        r = RT.ingest(idx, self.root, p, env="test")
        self.assertTrue(r["ok"])                                   # resilient: one bad line is skipped
        self.assertEqual(r["methods"], 1)

    def test_lambda_attributed_to_enclosing_method(self):
        idx = cli.load_index(self.root)
        # a synthetic lambda body should credit its enclosing method, not land in the unresolved bucket
        r = RT.ingest(idx, self.root, _trace(self.root, [
            {"t": "config", "env": "test"},
            {"t": "method", "qual": "Service.lambda$compute$0", "hits": 1},
            {"t": "method", "qual": "com.app.Generated$$Synthetic.x", "hits": 1},  # genuinely unresolved
        ]), env="test")
        confirmed = {m["qual"] for m in RT.load(self.root)["methods"]}
        self.assertIn("Service.compute", confirmed)               # lambda credited to compute
        self.assertEqual(r["unresolved"], 1)                      # only the synthetic one
        self.assertTrue(r["unresolved_top"])                      # histogram populated for diagnosis

    def test_freshness_staleness(self):
        idx = cli.load_index(self.root)
        RT.ingest(idx, self.root, _trace(self.root, [
            {"t": "config", "env": "test"},
            {"t": "method", "qual": "Service.compute", "hits": 1},
        ]), env="test")
        idx = self._idx()
        conf = {idx["rg"].nodes[a].qual for a in idx["rt_confirmed"]}
        self.assertIn("Service.compute", conf)                     # fresh → confirmed
        # mutate the method body → its anchor hash changes → no longer confirmed, but still "traced"
        _write(self.root, "pkg/svc.py", SVC.replace("return n * 2", "return n * 2 + 1  # edited"))
        idx2 = cli.fresh_index(self.root)                          # re-index + re-attach
        conf2 = {idx2["rg"].nodes[a].qual for a in idx2["rt_confirmed"] if a in idx2["rg"].nodes}
        self.assertNotIn("Service.compute", conf2)                 # changed → dropped from confirmed
        traced = {idx2["rg"].nodes[a].qual for a in idx2["rt_traced"] if a in idx2["rg"].nodes}
        self.assertIn("Service.compute", traced)                   # but still known to have run before


class MutationCoverageTest(unittest.TestCase):
    def setUp(self):
        self.root = _mk_repo()

    def test_field_mutation_before_after_and_coverage(self):
        idx = cli.load_index(self.root)
        # synthetic trace: compute executed; a setter mutation; an instrumented-but-unrun class
        RT.ingest(idx, self.root, _trace(self.root, [
            {"t": "config", "env": "prod"},
            {"t": "class", "name": "Service"},
            {"t": "method", "qual": "Service.compute", "hits": 1},
            {"t": "mutation", "node": "Service.compute", "kind": "field", "target": "Conf.mode",
             "op": "write", "before": "A", "after": "B", "n": 1},
        ]), env="prod")
        data = RT.load(self.root)
        self.assertEqual(len(data["mutations"]), 1)
        mu = data["mutations"][0]
        self.assertEqual((mu["before"], mu["after"]), ("A", "B"))
        self.assertIn("Service", data["instrumented_classes"])
        # coverage: compute executed; refund instrumented(Service)-but-never-ran
        idx2 = cli.fresh_index(self.root)
        cw = {n.id: n for n in idx2["rg"].nodes.values()}
        cid = next(i for i, n in cw.items() if n.qual == "Service.compute")
        rid = next(i for i, n in cw.items() if n.qual == "Service.refund")
        self.assertEqual(RT.coverage(idx2, self.root, cid)["status"], "executed")
        self.assertEqual(RT.coverage(idx2, self.root, rid)["status"], "instrumented")

    def test_mutation_writer_node_freshness(self):
        idx = cli.load_index(self.root)
        RT.ingest(idx, self.root, _trace(self.root, [
            {"t": "config", "env": "prod"},
            {"t": "method", "qual": "Service.compute", "hits": 1},
            {"t": "mutation", "node": "Service.compute", "kind": "field", "target": "x.y",
             "op": "write", "after": "v", "n": 1},
        ]), env="prod")
        self.assertEqual(len(cli.fresh_index(self.root)["rt_mutations"]), 1)
        _write(self.root, "pkg/svc.py", SVC.replace("return n * 2", "return n * 9"))  # writer changed
        self.assertEqual(len(cli.fresh_index(self.root)["rt_mutations"]), 0)           # stale → dropped


class MemoryTest(unittest.TestCase):
    def setUp(self):
        self.root = _mk_repo()
        self.idx = cli.load_index(self.root)

    def test_typed_memory_coexist_and_supersede(self):
        MEM.remember(self.idx, self.root, "computed eagerly for caching", ["Service.compute"], kind="mechanism")
        MEM.remember(self.idx, self.root, "should halve, not double", ["Service.compute"], kind="expectation")
        kinds = {e.get("kind") for e in MEM.load_memories(self.root)}
        self.assertEqual(kinds, {"mechanism", "expectation"})      # different kinds coexist on same anchor
        # same-kind write supersedes
        MEM.remember(self.idx, self.root, "actually computed for X", ["Service.compute"], kind="mechanism")
        mechs = [e for e in MEM.load_memories(self.root) if e.get("kind") == "mechanism"]
        self.assertEqual(len(mechs), 1)
        self.assertIn("for X", mechs[0]["fact"])

    def test_recall_filters_kind_and_flags_stale(self):
        MEM.remember(self.idx, self.root, "cheaper should win", ["Service.compute"], kind="expectation")
        exp = MEM.recall(self.idx, self.root, anchors={"pkg/svc.py::Service.compute"}, kinds={"expectation"})
        self.assertEqual(len(exp), 1)
        self.assertEqual(exp[0]["kind"], "expectation")
        self.assertEqual(exp[0]["status"], "active")
        # edit the cited code → recall flags it stale, not active
        _write(self.root, "pkg/svc.py", SVC.replace("return n * 2", "return n * 3"))
        idx2 = cli.fresh_index(self.root)
        exp2 = MEM.recall(idx2, self.root, anchors={"pkg/svc.py::Service.compute"}, kinds={"expectation"})
        self.assertTrue(exp2 and exp2[0]["status"] == "stale")

    def test_unanchorable_fact_refused(self):
        r = MEM.remember(self.idx, self.root, "a floating claim", ["no_such_symbol_xyz"])
        self.assertFalse(r["stored"])                              # anchor-or-drop

    def test_recall_no_embed_fallback_never_loads_model(self):
        # a memory with NO anchor/file overlap would normally hit the embedding fallback; with
        # embed_fallback=False it must return [] WITHOUT importing/calling embeddings (explain's path).
        MEM.remember(self.idx, self.root, "unrelated fact", ["Service.compute"])
        import vard.embed as E
        orig = E.embed_texts
        E.embed_texts = lambda *a, **k: (_ for _ in ()).throw(AssertionError("embeddings must NOT load"))
        try:
            out = MEM.recall(self.idx, self.root, anchors=set(), files=set(),
                             query="something with no anchor overlap", embed_fallback=False)
            self.assertEqual(out, [])
        finally:
            E.embed_texts = orig


class RankInvariantTest(unittest.TestCase):
    """The never-demote invariant: turning runtime ON can only raise a node's score, never lower it."""
    def setUp(self):
        self.root = _mk_repo()
        self.idx = cli.load_index(self.root)
        RT.ingest(self.idx, self.root, _trace(self.root, [
            {"t": "config", "env": "test"},
            {"t": "method", "qual": "Service.compute", "hits": 4},
            {"t": "edge", "caller": "Service.handle", "callee": "Service.compute", "n": 4},
        ]), env="test")
        RT.attach(self.idx, self.root)

    def test_runtime_never_demotes(self):
        rg = self.idx["rg"]
        nodes = [n for n in rg.nodes.values() if n.type in ("function", "method", "class")]
        off, _ = RK.rank_nodes(self.idx, "compute the value", self.root, nodes, runtime_mode="off")
        for mode in ("fused", "prior", "tag"):
            on, _ = RK.rank_nodes(self.idx, "compute the value", self.root, nodes, runtime_mode=mode)
            for nid in off:
                self.assertGreaterEqual(on[nid] + 1e-9, off[nid],
                                        f"mode={mode} demoted {nid}: {on[nid]} < {off[nid]}")

    def test_modes_resolve(self):
        self.assertEqual(RK.resolve_runtime_mode(self.idx, "off"), "off")
        self.assertEqual(RK.resolve_runtime_mode(self.idx, None), "fused")   # trace exists → auto fused
        self.assertEqual(RK.resolve_runtime_mode({}, None), "off")           # no trace → off


class ObservedConfigTest(unittest.TestCase):
    """observed-live config value extraction is pure logic over captured value samples."""
    def test_extracts_live_value_from_getter_sample(self):
        idx = {
            "config": {"app.mode": {"defs": [{"key": "app.mode", "value": "FileVal", "file": "x", "line": 1}],
                                    "readers": []}},
            "rt_values": {"pkg/conf.py::Conf.get": [
                {"v": '("app.mode") => "OVERRIDE"', "n": 2, "envs": {"prod": 2}}]},
        }
        out = RT.observed_config_values(idx)
        self.assertIn("app.mode", out)
        self.assertEqual(out["app.mode"]["value"], "OVERRIDE")
        self.assertEqual(out["app.mode"]["envs"], {"prod": 2})

    def test_ignores_non_config_args(self):
        idx = {"config": {"app.mode": {"defs": [{"key": "app.mode", "value": "x", "file": "x", "line": 1}]}},
               "rt_values": {"a::b": [{"v": '("unrelated.arg") => "z"', "n": 1, "envs": {}}]}}
        self.assertEqual(RT.observed_config_values(idx), {})

    def test_key_as_non_first_arg(self):
        # a getter like get(scope, key) — the key is the SECOND arg; must still be found
        idx = {"config": {"app.mode": {"defs": [{"key": "app.mode", "value": "x", "file": "x", "line": 1}]}},
               "rt_values": {"a::b": [{"v": '("GLOBAL", "app.mode") => "LIVE"', "n": 1, "envs": {"prod": 1}}]}}
        out = RT.observed_config_values(idx)
        self.assertEqual(out.get("app.mode", {}).get("value"), "LIVE")


class ExplainTest(unittest.TestCase):
    def setUp(self):
        self.root = _mk_repo()

    def test_sections_and_no_trace_degrades(self):
        txt = MEM.explain(cli.fresh_index(self.root), self.root, "Service")
        self.assertIn("## ACTUAL", txt)
        self.assertIn("## MECHANISM", txt)
        self.assertIn("no runtime trace", txt)                    # honest when ungrounded

    def test_expected_but_never_observed_divergence(self):
        idx = cli.load_index(self.root)
        MEM.remember(idx, self.root, "refunds must re-credit", ["Service.refund"], kind="expectation")
        # trace exercises handle/compute but NOT refund
        RT.ingest(idx, self.root, _trace(self.root, [
            {"t": "config", "env": "test"},
            {"t": "method", "qual": "Service.handle", "hits": 1},
            {"t": "method", "qual": "Service.compute", "hits": 1},
        ]), env="test")
        txt = MEM.explain(cli.fresh_index(self.root), self.root, "Service")
        self.assertIn("DIVERGENCE", txt)
        self.assertIn("refund", txt)
        self.assertIn("NEVER", txt)                               # expected-but-never-observed fired


if __name__ == "__main__":
    unittest.main()
