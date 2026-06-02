# VARD eval harness (research)

Measures **VARD as the dependent variable**. Everything is fixed (the task set, the agent's
conventional search) except the retrieval layer, which we ablate: `off` → `codefirst` →
`coupling` → (next) `state-lineage`.

## The instrument: dark-gold recall

A retrieval layer can only beat the agent on code the agent's own search can't reach. So we
measure exactly that.

1. **Gold** = the spans the accepted fix changed (reconstructed from `git diff base..fix` on the
   base commit), mapped to their enclosing symbols.
2. **Conventional reach** = three channels, using VARD's *own* implementations so it's an honest
   bar, not a strawman:
   - lexical (BM25 over method passages) ~ grep
   - semantic (embeddings over the same passages) ~ embedding search
   - structural (import-graph reachability from issue-mentioned symbols) ~ follow-the-imports
3. **Dark gold** = gold symbols reached by *none* of the three at top-k.
4. **Score** = `|dark ∩ retriever_topk| / |dark|` — marginal recall on dark gold.

Expected: `codefirst` recovers ~0% of dark gold by construction (it only re-fuses the three
channels). That number is the quantitative form of "VARD ≈ the agent's own search." The thesis
is that a state-aware signal recovers dark gold. `coupling` is the first test of that; the
state-lineage graph is next.

`logic`-class bugs are the **negative control** — a state layer should add ~nothing there, and
reporting that honestly sizes the addressable slice.

## Selection protocol (do this BEFORE running anything)

- Pick bugs without looking at any retriever's output. No cherry-picking to win.
- Obscure, actively-maintained, non-tiny Java/Spring repos (beats the memorization confound).
- Fix touches ≥2 files.
- Label `bug_class`: `coupling` (a state write on one side, a read on another, no direct call
  between them) vs `logic` (control flow / pure computation).

## Add a bug

Copy `_TEMPLATE.json` to `eval/bugs/<id>.json`, fill repo + the two SHAs + issue text + class.
Gold is auto-derived from the diff; override with an explicit `"gold": [{file,start,end}]` only
when the diff is noisy (formatting-only hunks, generated code).

## Run

```bash
python -m eval.run --self-test            # plumbing check on VARD's own repo (not a result)
python -m eval.run eval/bugs/*.json       # all retrievers
python -m eval.run --retriever coupling --retriever codefirst eval/bugs/*.json
```

Repos cache under `~/.vard-eval/repos` (override with `VARD_EVAL_REPOS`).

## What "done" looks like for step 1

A table: per-bug dark-gold count, and each retriever's marginal recall on it, split by
coupling vs logic. Once that baseline exists, we hand-build state lineage on the coupling
subset and see if it clears the bar before writing any extraction code.
