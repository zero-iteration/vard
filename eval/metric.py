"""Dark-gold metric.

gold spans -> gold symbols (smallest enclosing node per span)
dark gold  = gold symbols reachable by NONE of {lexical@k, semantic@k, structural}
recall of a retriever on dark gold = |dark ∩ retriever_topk| / |dark|

The headline number this exists to produce: marginal recall on dark gold. A retriever that
only re-fuses the three conventional channels (e.g. code-first VARD) is expected to score ~0
here by construction — that is the quantitative statement of "VARD ≈ the agent's own search."
A signal that recovers dark gold (coupling layer, or the state-lineage graph) is the thesis.
"""
from dataclasses import dataclass
from . import channels as CH


@dataclass
class BugResult:
    bug_id: str
    bug_class: str
    n_gold_spans: int
    n_gold_syms: int
    n_dark: int
    reach_lex: int          # gold syms reached by lexical@k
    reach_sem: int
    reach_struct: int
    retriever_dark_hits: int
    retriever_total_hits: int

    @property
    def dark_recall(self):
        return self.retriever_dark_hits / self.n_dark if self.n_dark else None

    @property
    def gold_recall(self):
        return self.retriever_total_hits / self.n_gold_syms if self.n_gold_syms else None


def gold_symbols(rg, gold_spans):
    """Smallest enclosing graph node for each gold span, restricted to real fix sites.

    A span that maps only to the file-level `<module>` node is an import/package-line change —
    a *consequence* of the fix, never a localization target — so it is dropped. Class- and
    method-level nodes are kept (a fix can legitimately live in a class body, e.g. an annotation
    or field on a cached DTO)."""
    ids = set()
    for s in gold_spans:
        cands = [n for n in rg.nodes_for_span(s.file, s.start, s.end) if n.type != "module"]
        if cands:
            ids.add(cands[0].id)
    return ids


def evaluate(bug, idx, retriever_topk_ids, k_channel=10):
    """idx: a built VARD index dict for bug.repo_dir at the base commit.
    retriever_topk_ids: set of node ids the retriever-under-test returned."""
    rg = idx["rg"]
    nodes = CH.content_nodes(rg)
    gsyms = gold_symbols(rg, bug.gold)

    chunks, keys, bm = CH.chunk_index(nodes, bug.repo_dir)
    lex = CH.topk_ids(CH.lexical_scores(bug.issue_text, keys, bm), k_channel)
    sem = CH.topk_ids(CH.semantic_scores(bug.issue_text, nodes, keys, chunks, bug.repo_dir), k_channel)
    seed_files = {rg.nodes[i].file for i in (lex | sem)}      # files the agent's search surfaced
    reach_files = CH.structural_reach_files(idx, seed_files, hops=1)  # follow their imports 1 hop
    struct = {n.id for n in nodes if n.file in reach_files}

    conventional = lex | sem | struct
    dark = gsyms - conventional

    return BugResult(
        bug_id=bug.id, bug_class=bug.bug_class,
        n_gold_spans=len(bug.gold), n_gold_syms=len(gsyms), n_dark=len(dark),
        reach_lex=len(gsyms & lex), reach_sem=len(gsyms & sem), reach_struct=len(gsyms & struct),
        retriever_dark_hits=len(dark & retriever_topk_ids),
        retriever_total_hits=len(gsyms & retriever_topk_ids),
    )
