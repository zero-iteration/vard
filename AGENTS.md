# Using VARD (for AI coding agents)

If you are an AI coding agent working in a project that has VARD available (the `vard` command is on
PATH, or a `.vard/` index exists), read this once and wire yourself up.

## One-time setup

```bash
vard init     # indexes the repo, writes the routing block to CLAUDE.md/AGENTS.md, and registers the MCP server
```

That's it — `vard init` wires everything (idempotent; safe to re-run). The routing block then lives in
CLAUDE.md/AGENTS.md, so you apply these rules automatically every session. Restart the agent once so the
MCP tools load. (`vard rules --write` re-applies just the routing block if you ever need it.)

## How to use it (the whole interface — don't invent commands)

| when | call |
|---|---|
| locate code — "where is X", "what handles Y", understand a feature, gather context before a change | `vard_context("<task in plain words>")` (CLI: `vard context "..."`) — use its `file:line` spans as your starting set, **before** grepping |
| before editing code that touches shared state | `vard_impact("<QualifiedName or file.py:line>")` — readers/writers coupled through caches/DBs/queues |
| data is wrong / stale / incomplete and the code that sets it isn't obvious | `vard_state_candidates("<task>")` → identify which state types are wrong (incl. ones the symptom doesn't name) → `vard_state_lineage("TypeA, TypeB")` for the code that defines + produces/consumes that state |
| the WHOLE picture before editing / when you need full context | `vard_whole_picture("<Class or file>")` — code + state it touches + coupled code + the decisions/tickets/incidents behind it (why, from history) + what co-changes with it (context you can't reconstruct from the code) |
| trace a data resource | `vard_resource("<table / cache-key / queue>")` — who writes vs reads it |
| see hidden couplings | `vard_couplings` |

There is **no** `search`, `stats`, `lint`, or `readers` command — the table above is the entire surface.
Do not read VARD's own source to reverse-engineer the interface; this file is it.

## Good to know

- VARD finds context; it does not write code. It is a retrieval layer — you still do the reasoning and edits.
- It returns precise `file:line` spans, not whole files. Open exactly those ranges first.
- Languages: Python, Java, JS/TS, Go. On other languages (e.g. C++) it indexes nothing useful — fall back to normal search.
- `couplings` may be empty on apps whose shared state lives behind external services — that's expected, not a failure. Localization still works.
