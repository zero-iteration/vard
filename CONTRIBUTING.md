# Contributing to VARD

Thanks for looking. VARD is a small, focused codebase (~2k LOC) — easy to read end to end.

## Dev setup

```bash
git clone https://github.com/zero-iteration/vard && cd vard
python -m venv .venv && source .venv/bin/activate
pip install -e ".[all]"          # all extras: embeddings + openai + mcp + learn
```

Python 3.10+.

## Project layout

| file | role |
|---|---|
| `vard/languages/` | tree-sitter providers → uniform symbols + call-sites (the only language-specific code) |
| `vard/graph.py` | symbol graph (nodes + `contains`/`inherits` edges) |
| `vard/resources.py` | data-coupling layer (writer⇄reader through cache/DB/queue) |
| `vard/embed.py` | passage chunking + cached embeddings |
| `vard/propagate.py` | import graph + personalized PageRank |
| `vard/history.py` | commit-history candidate source |
| `vard/rank.py` | scoring core — combines every signal |
| `vard/selflabel.py` | optional per-repo weight learning (`vard learn`) |
| `vard/freshness.py` | incremental indexing |
| `vard/cli.py` | `build_index`, `context_text`, commands |
| `vard/mcp_server.py`, `vard/hook.py` | agent integration |

## Try it

```bash
vard init .
vard context "describe a bug here"
```

## Sanity checks before a PR

There's no formal test suite yet. At minimum:

```bash
python -m compileall vard            # everything compiles
vard init . && vard context "x"      # runs end to end on this repo
```

Please also run your change against a real repo with embeddings on **and** with
`VARD_EMB_MODEL=none` (BM25-only) — both paths must work and degrade gracefully.

## Adding a language

Add one entry to the `LANGS` config in `vard/languages/treesitter_provider.py`
(extension, tree-sitter grammar name, node-type names for containers/functions/calls/imports).
Everything above the providers is language-agnostic.

## Design principles (learned the hard way — please keep)

- **Complexity must earn its place.** Several "smarter" approaches (a GNN router, naive
  signal fusion) lost to simpler ones in testing. Measure before adding machinery.
- **Never degrade silently.** If embeddings/a layer/a file fails, warn and continue — never
  crash, never quietly run worse. A bad file must not kill `vard init`.
- **VARD finds context; the model solves.** Keep the scope a retrieval layer. Resolution,
  patching, and agent loops are out of scope.
- **Local and key-optional by default.** Any network/paid call must be opt-in and announced.

## PRs

Keep them small and focused. Describe what you changed and how you verified it. No hype in
code comments or docs — say what it does and why.

## License

By contributing you agree your contributions are licensed under the MIT License.
