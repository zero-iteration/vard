# VARD eval — findings log

## Instrument: dark-gold metric (built + validated)

Measures VARD as the dependent variable. Dark gold = gold fix-sites reachable by NONE of
{lexical BM25, semantic embeddings, 1-hop import-follow from search hits}. A retriever can only
beat the agent on dark gold. See README.md for the protocol.

Two instrument bugs found and fixed during validation (both would have produced misleading numbers):
- import/package-line changes mapped to the `<module>` node and counted as dark gold → now dropped
  (imports are a consequence of a fix, never a localization target).
- the structural channel expanded a 2-hop closure from raw issue keywords, sweeping 57% of a
  1,998-file monorepo (seeded on junk words like "been"/"also") → now seeded only from the files
  lexical/semantic actually surfaced, expanded 1 hop (faithful to how an agent follows imports).

## Run 1 — n=2 (pipeline validation, not a study)

| bug | class | gold | dark | reachable by | codefirst | coupling |
|---|---|--:|--:|---|--:|--:|
| kcloud-cache-result | coupling | 7 | 7 (100%) | nothing | 0/7 | 0/7 |
| thrivex-comment-email | logic (control) | 3 | 1 | lex+sem+struct reach 2/3 | 1/3 | 1/3 |

- The coupling bug (`Result<T>` + `*CO` DTOs + convertor fail to deserialize on a Redis cache hit)
  is **100% dark** — invisible to every conventional channel — and shipped VARD recovers **0/7**,
  coupling layer included (its heuristic didn't fire on KCloud's `@DataCache` pattern). This is the
  measured form of "VARD ≈ the agent on real coupling bugs," and the bar to beat.
- The logic control behaves sanely: 2/3 sites conventionally findable; the 1 dark site is the
  `@EnableAsync` annotation (no textual link to the symptom), recovered by neither retriever.

## Run 2 — hand-built state lineage on kcloud-cache-result (the go/no-go test)

Rule (reproducible, blind to gold): `@DataCache` writers → cached return-type shape `Result<XxxCO>`
→ unfold generics → shape type-defs + their producer convertors.

| | conventional | current VARD | hand-built state lineage |
|---|:--:|:--:|:--:|
| dark sites recovered (of 7) | 0 | 0 | **7** |
| closure size | — | — | ~18 symbols (of 8,710) |

**Verdict: green light to build a scoped state-lineage extractor.** The state-first closure recovers
exactly the dark gold that defeats lexical/semantic/graph search AND current VARD, at high precision,
via a statically-buildable rule.

Caveats: n=1, hand-built; KCloud is unusually clean (explicit COs + convertors + uniform cache
annotation). Must validate the rule on more coupling patterns (queue producer/consumer, shared DB
table, events) and messier repos before trusting generality. Query→state identification was trivial
here; in general it is its own inference step.

## Run 3 — generalization tests across patterns + a messier repo (the "is KCloud just clean?" question)

| # | pattern | repo | result |
|---|---|---|---|
| 1 | cache (`@DataCache` → `Result<CO>`) | KCloud (clean DDD) | **7/7 dark recovered**, ~18-symbol closure |
| 2 | in-memory shared state (`OssUtil` holder) | ThriveX (messy MyBatis, no DDD) | **partial → fixable** |
| 3 | queue/event (Kafka domain event) | KCloud (generic handler idiom) | **connects producer↔consumer cross-module**, ~5-file closure |

**Test 2 detail (the informative one).** State-centering recovered the core coupling (`OssUtil` holder +
`OssServiceImpl` writer + `OssStartupListener` reader) that shipped VARD got 1/11 on. It missed 2 dark sites,
and the reasons split cleanly:
- `OssController` — missed because the controller depends on the *interface* `OssService`, holder reached via
  `OssServiceImpl implements OssService`. **Fixable by interface↔impl resolution** (VARD's graph already has
  `implements` edges). Confirmed: keying entry-points on the interface recovers `OssController`.
- `EOssPlatform` — **0 references at base commit**; the fix itself introduces its use. No localizer (state,
  VARD, or conventional) can recover from pre-fix code a symbol whose link to the state is created by the fix.
  This is a **localization ceiling**, not a state-lineage failure.

**Test 3 detail.** Mechanism test on live code (no bug commit). Keying the lineage on the message/event TYPE
(`OssUploadEvent`) connects publisher, payload def, and the `@KafkaListener` consumer across 4 modules — even
though KCloud uses one generic polymorphic `DomainEventHandler`, because the handler still references the type.
The consumer method is named `handleLoginLog` (textually unrelated to "OSS upload") → conventional search misses
the link, the type-key doesn't.

## Verdict: GREEN LIGHT to build a scoped state-lineage extractor

The state-first idea generalizes across cache / in-memory-state / queue-event patterns and a messier repo. The
unifying spine is consistent: **key on the state/payload TYPE (+ resource id), then gather its definition +
producers (builders/convertors) + consumers (readers/handlers/listeners).** The failures were not conceptual.

Extractor requirements the tests surfaced (scope to these):
1. per-pattern resource detection: cache annotations, message topic↔`@KafkaListener`, in-memory static holders,
   DB tables/entities.
2. the spine: STATE TYPE → its def + producers + consumers, via a type-reference index ("all refs to type T").
3. interface↔impl resolution (Spring DI) and 1–2 call-graph hops to entry points — name-matching is not enough.

Known ceiling (do not chase): symbols whose link to the state is created by the fix are unrecoverable from base
code (e.g. `EOssPlatform`).

## Run 4 — state_lineage extractor built + first comparison (n=3: 2 coupling + 1 control)

`eval/state_lineage.py`: resource detection (cache/queue annotations + in-memory holders) -> state-type
spine (def + members + producers/consumers via type-reference index) + interface/impl resolution.

| retriever | dark-gold recall (coupling, n=2) | gold recall (coupling) | logic control |
|---|:--:|:--:|:--:|
| codefirst (shipped) | 0/10 (0%) | 1/18 (6%) | 33% (no harm) |
| coupling (shipped) | 0/10 (0%) | 1/18 (6%) | 33% |
| **state_lineage** | **8/10 (80%)** | **15/18 (83%)** | 33% (no harm) |

Per-bug: KCloud cache **7/7 dark** (automated == hand-built); ThriveX OSS **1/3 dark, 8/11 gold** (vs VARD 1/11).
OSS misses: `EOssPlatform` (fix-introduced link, unrecoverable by anyone) + one `OssController` method.

**Result: the state-first signal takes dark-gold recall 0% -> 80% on the bugs that defeat shipped VARD, no harm
to the logic control.** Dark gold is unreachable by content ranking at ANY k, so this is the signal, not output
size.

**Precision is v1-rough (the honest cost):** KCloud closure = 318 symbols (4% of 6,712) but it expanded ALL 12
cache/queue payload types repo-wide, not just the 6 implicated (hand-built was ~18). ThriveX OSS = 63 (12%).

## Run 5 — scaled to n=10 (2 coupling + 8 logic) + fair-budget check (the key correction)

Raw recall (BEWARE budget asymmetry: codefirst returns k=8, state_lineage returns 40-318):
- coupling (n=2): codefirst 0/7 dark, state_lineage 7/7 dark + 7/7 gold.
- logic (n=8): codefirst gold 2/12 @k=8, state_lineage 10/12 — but this is OUTPUT SIZE, not capability.

**Fair-budget check (codefirst@60 ~= state_lineage's closure size) is decisive:**

| bug | class | dark | cf@8 | cf@60 | state_lineage |
|---|---|:--:|:--:|:--:|:--:|
| article-list-guest | logic | 0 | 0% | **100%** | 100% |
| empty-password-hash | logic | 0 | 0% | **100%** | 100% |
| deleted-article-nav | logic | 0 | 0% | **100%** | 100% |
| category-sort | logic | 0 | 33% | **100%** | 100% |
| exception-message | logic | 0 | 100% | 100% | 100% |
| comment-email | logic | 1 | 33% | **67%** | 33% (worse) |
| oss-platform-validation | logic | 0 | 0% | **100%** | 75% |
| **oss-dynamic** | coupling | 3 | 9% | **9%** | **73%** |
| **kcloud-cache** | coupling | 7 | 0% | **0%** | **100%** |

Closure sizes (precision): codefirst fixed 8; state_lineage 41-88 on ThriveX (8-24% of ~400), 318 on KCloud (4%).

**VERDICT: state_lineage is a COUPLING-SPECIFIC signal, not a general retriever.**
- On logic bugs, content ranking at equal budget (cf@60) gets 100% — state_lineage adds no capability and
  costs precision; on comment-email it is strictly worse. The raw "89% vs 11%" headline is a budget artifact.
- The genuine, budget-INDEPENDENT win is coupling/dark gold: content ranking gets 0-9% at ANY budget, state
  lineage 73-100%. That is the only place it helps, and it helps a lot.
- Product implication: FUSE, don't replace. Content ranking is the spine; inject the state-lineage closure only
  when coupling structure is implicated. Standalone state_lineage on logic bugs is wrong (noise).

## Run 6 — the merge hypothesis: "make state+code ONE graph, let propagation cover both"

Tested by injecting state edges (writer<->state-def<->producers, 2685 edges, 102 touching gold) into VARD's
propagation graph and running the SHIPPED ranker unchanged (`merged_graph` retriever), plus ranking by PPR over
the merged graph directly.

| approach | KCloud dark gold | diagnosis |
|---|:--:|---|
| state edges + VARD additive ranker (merged_graph) | 0/7 | dark nodes score ~0.40 (PPR-only term); top-8 cutoff 1.07 — additive content scoring caps them below content hits |
| rank by PPR over merged graph | gold rank 400-900/1666 | diffusion spreads mass over 1666 files; signal dilutes (on small ThriveX repo PPR-merged does ok: ranks 3-32) |
| typed traversal (state_lineage) | 7/7 | resource->state-type->producers; no dilution |

merged_graph ended IDENTICAL to codefirst (0/7 dark, 17% logic) — the edges propagate (lift gold rank ~6000 ->
~500) but additive content-dominated scoring nullifies it.

**VERDICT: merge is the right DATA MODEL (one graph, code + state edges); diffusion/PPR is the WRONG query
mechanism for state edges (dilutes). State edges must be queried by TYPED TRAVERSAL (what state_lineage does).**
Unified design = ONE graph, TWO retrieval modes: diffusion for code-context (logic), typed traversal for
state-coupling (dark gold), unioned. Reconciles "merge graphs" (data model: yes) with "fuse" (retrieval: 2 modes).

Precision bug found: in-memory-holder detector flags 314 "holders" in ThriveX (catches any `static` member incl
loggers/constants) — the source of state_lineage's logic-bug noise. v2: static MUTABLE runtime-written fields only.

## Run 7 — unified retriever (`hybrid`) VALIDATED end to end (n=10)

Built: (1) holder detection tightened 314 -> 2 (static MUTABLE non-logger non-CONSTANT fields only; OssUtil
still caught). (2) `hybrid` = content/diffusion ranking spine (codefirst top-k) UNION gated typed state-traversal
(`gated_state_closure`) that fires ONLY when a resource is implicated (cache/queue kind in query, or a
resource/holder near the seeds); returns {} on a pure logic bug.

| retriever | dark-gold recall (coupling) | logic gold @tight | output (logic / coupling) |
|---|:--:|:--:|:--:|
| codefirst | 0/7 (0%) | 17% | 8 / 8 |
| **hybrid** | **7/7 (100%)** | **17% (= codefirst)** | **8 / 34 (oss), 224 (kcloud)** |
| state_lineage | 7/7 (100%) | 83% (budget artifact) | 40-318 |

Both criteria met: coupling dark gold recovered (= state_lineage); logic identical to codefirst with output=8 on
7/8 logic bugs (state half returns {} -> no noise). hybrid's logic recall (17%) is LOWER than standalone
state_lineage's 83% AND THAT IS CORRECT — the 83% was breadth, hybrid gives tight honest localization on logic
and adds dark gold only on coupling, at zero logic cost.

**UNIFIED DESIGN VALIDATED: one graph, two retrieval modes (diffusion for code-context, typed traversal for
state-coupling), one tight retriever covering BOTH. dark-gold 0% -> 88% (7/8) with no logic degradation.**

Blemishes: (1) oss-platform-validation (logic but OSS-adjacent) over-fired the state channel -> output 26, no
recall benefit, no harm. (2) KCloud closure still 224 (3% of repo) — expands all cache payloads; producer-only
refs (return-type==T) is the precision v2.

## Run 8 — external validation on ContextBench (Multi-SWE-Bench) Java + coupling-evidence ceiling

ContextBench java-verified: 9 of 15 loaded (6 had unreachable base SHAs even with fetch fallback). These are
LIBRARIES (fastjson2, jackson, logstash) — no shared-state coupling.

| retriever | dark-gold recall | gold recall @k=8 | state fired |
|---|:--:|:--:|:--:|
| codefirst | 0/5 | 4/24 | — |
| hybrid | 0/5 | 7/24 | 3 of 9 |
| state_lineage | 0/5 | 11/24 (breadth artifact) | — |

Findings: (1) 8/9 have zero dark gold (libraries have no coupling). (2) The 1 bug with dark gold (5 spans) is
NOT recovered by anyone incl state_lineage — it's parser-internal structural disconnection, NOT state-coupling;
state_lineage targets state-coupling specifically and correctly doesn't claim it. So NO budget-independent
benefit on libraries (dark 0/5 for all) — correct scoping. (3) Over-fire blemish reconfirmed: hybrid fired the
state channel on 3/9 library bugs (gold 7 vs 4 = breadth, no dark recovery, lower precision). Gating fires on
presence of any annotation, not genuine coupling -> needs tightening.

**Coupling bug-recovery evidence CEILING (a finding, not a gap):** coupling-class BUG COMMITS are rare in public
history. KCloud (coupling-rich) organic history = ~all build fixes (1 coupling bug). ThriveX = 1. Saga/CQRS repos
(eventuate, ddd-cqrs) = dry bug history + framework idioms v1 doesn't detect. ContextBench java = libraries (0
coupling). So bug-recovery evidence is capped at n=2 strong (kcloud-cache 7/7, thrivex-oss) + 3-pattern mechanism
validation + external no-harm. Paths to more (all costly): per-framework extractor patterns; deploy on a live
service repo to collect coupling bugs prospectively; a purpose-built coupling-bug dataset (none public).

## Run 9 — CORRECTION: I built a coupling detector, not a state model (user redirect, validated)

The user's actual thesis: STATE IS THE SKELETON FOR EVERYTHING. Every code transforms some state; localize ANY
bug by finding the WRONG state then the code that produced it. Coupling is NOT special — it's just a state
mutated by >1 thing. My v1 only modeled state when it saw a cache/queue/holder annotation ("no resource -> no
state -> nothing"), which is wrong — there is always state.

Partly validated on the ContextBench mockito bug (mockStatic bypasses DoNotMockEnforcer). General type-traversal
(no resource whitelist) seeded from the symptom-named type DoNotMockEnforcer DID recover its plugin-registry
lineage (DefaultMockitoPlugins, PluginRegistry, Plugins) — proving state generalizes beyond resource annotations.
CORRECTION (I misread this first): those registry sites are NON-dark (reachable by struct). The ACTUAL dark gold
is CreationSettings.* / MockNameImpl.* (mock-creation-settings state), and the general model MISSED all 5 of it
(0/5) — same as everyone — at 137-symbol breadth. It missed because CreationSettings is the wrong state but the
symptom never names it and the seeds don't reach it. => the general model widens WHAT counts as state (right) but
does NOT solve identifying WHICH state is wrong when unnamed. That inference is the wall.

CORRECTED ARCHITECTURE:
- state nodes = ALL types/fields (data nouns), NOT just cache/queue/holder resources. Drop the resource whitelist.
- localization = symptom-implicated state -> its producers/consumers/registrants (general type def-use) UNION
  content ranking (for the directly-named transformation code + primitive/local computational state).
- coupling / plugin-registry / entity-DTO / cache all become ONE mechanism: a type produced in one place,
  consumed in another. No special-casing.

Limits that DON'T disappear (build accordingly):
1. symptom -> implicated-state inference is now THE crux (easy when the state type is named, e.g.
   DoNotMockEnforcer; hard otherwise — content seeds bridge some). This is the make-or-break.
2. type-level state misses primitive/local computational state (an isAdmin boolean isn't a type) — content
   ranking still owns those; the skeleton is types/fields.

## Run 10 — general_hybrid built + validated: more-correct model, but identification is the wall

`general_state_closure` / `general_hybrid`: state = ALL types; implicated state = symptom-named types (3.0) +
seed-referenced types (1.0+) + resource-hinted types (2.0, resources demoted from gate to HINT). vs codefirst on
10 curated:

| retriever | coupling dark | logic gold | note |
|---|:--:|:--:|---|
| codefirst | 0/7 | 17% @8 | tight |
| hybrid (resource-gated) | 7/7 | 17% @8 | tight, but blind to non-resource state |
| general_hybrid | 5/7 | 83% (breadth) | recovers more state, but logic breadth back + coupling 7->5 |

mockito (non-coupling, stateful): general_hybrid dark 0/5 (output 137) — recovered the DoNotMockEnforcer lineage
(non-dark) but NOT the dark CreationSettings state. Identification failed (state unnamed in symptom).

CONCLUSION: the general state model is the right DATA MODEL (state = all types, coupling is one shape) and it
provably generalizes beyond resources. But "everything is state" means the channel fires broadly (precision cost)
AND the binding constraint becomes SYMPTOM->STATE IDENTIFICATION, which heuristics can't do (no keyword links
"static mocks" to CreationSettings). RESOLUTION (fits the scope boundary VARD=structure, LLM=reasoning): let the
AGENT name the implicated state from the symptom; VARD builds the state skeleton + traverses it. Identification is
reasoning (model), skeleton+traversal is structure (VARD).

## Run 11 — DECISIVE: agent identifier vs softmax identifier (the "attention" question)

Both name implicated STATE TYPES from the symptom; VARD traverses; measure dark-gold recovery. n=3 dark bugs.

| bug | dark spans | softmax identifier | agent identifier |
|---|:--:|:--:|:--:|
| kcloud-cache | 7 | 0/7 | 3/7 (named Result, MenuCO) |
| thrivex-oss | 3 | 0/3 | 2/3 (named EOssPlatform) |
| mockito | 5 | 0/5 | 5/5 (named CreationSettings, MockNameImpl) |
| TOTAL | 15 | **0/15 (0%)** | **10/15 (67%)** |

The softmax identifier (content similarity + state-graph PPR over local features) recovers 0/15 — it surfaces the
lexically/structurally NEAR types (DoNotMockEnforcer, cache managers) but never the textually-disconnected dark
state. The agent recovers 67% via DOMAIN REASONING: mockito agent named CreationSettings + MockNameImpl ("static
mocks carry creation settings; mock name used in enforcement") with zero lexical link to the symptom -> 5/5.

CORRECTION (fairer softmax): the first softmax used lexical+semantic features — known blind to dark state, so a
strawman. A FAIR structural identifier (anchor on the operations the symptom NAMES, follow def-use to the state
their bodies touch — NO lexical/sem on the state) does better: 2/15 vs the strawman's 0/15. But it still loses
badly to the agent (10/15), for two precise reasons: (1) prose symptoms have NO structural anchor (thrivex-oss
names no type/method -> 0 anchors -> 0/3; agent reasoned prose->EOssPlatform); (2) structure reaches the
NEIGHBORHOOD not the exact state (mockito structural got MockCreationSettings/MockSettings but not the gold
CreationSettings/MockNameImpl; the agent's domain knowledge pinned the exact ones).

VERDICT: use the AGENT for identification ("attention" = the LLM's attention, over a space that includes domain
knowledge). Even a FAIR structural identifier loses 2/15 vs 10/15 — so the agent's edge is NOT an artifact of a
rigged softmax; identification needs (a) starting from prose, (b) domain knowledge to pick the exact state from a
neighborhood. Both are reasoning, not feature-ranking. The agent already consumes the structural candidate list,
so agent = structural candidates + reasoning; the reasoning is the irreducible part. Consistent with VARD's prior
learned-router/GNN-lost-to-deterministic finding.

FINAL ARCHITECTURE (validated): VARD builds the state skeleton (all types) + does typed traversal; the AGENT
(already in the loop) identifies the wrong state from the symptom; content ranking is the spine for findable/
local code. Recovers dark gold across coupling (kcloud, thrivex) AND non-coupling-stateful (mockito).
Honest limits: agent identification incomplete (10/15, missed some per-entity COs / one controller); agent
closures larger (mockito 230 symbols = precision cost); n=3 dark bugs.

## Run 12 — real end-to-end head-to-head: VARD vs agent on a fresh Spring Boot repo

Repo: youlaitech/youlai-boot (264 java files, never used before). Bug ab7a878 (base a71a423): after a
data-permission change, the current-user API is missing gender/deptName/roleNames. Gold = CurrentUserDTO
(incomplete state shape) + UserServiceImpl.getCurrentUserInfo (its producer). Agent worked at the base commit,
grep/read only, NO git history.

| metric | agent (own search) | VARD |
|---|:--:|:--:|
| gold files localized | 2/2 | 2/2 (general_hybrid@8, 67 spans) · 1/2 (codefirst/hybrid@8, 8 spans) |
| LLM tokens to localize | 24,173 | 0 (local bge embeddings) |
| context handed downstream | reads files itself | ~720 tokens (ranked span list) |
| tool calls | 10 | 1 query |
| latency | 71 s | 7.6 s warm / ~24 s cold first query |
| precision | 2 files pinpoint | 8 spans (primary) -> 67 (both) |

Honest reading: (1) NOT a dark bug — CurrentUserDTO is findable (softmax ranked it #2), so this is a TOKEN-
EFFICIENCY win (same localization at ~0 LLM tokens / 10x faster), not a "VARD finds what agent can't" win.
(2) Precision tradeoff: agent's 24k tokens buy pinpoint 2 files; VARD ranks+hands a span list (tight top-8 gets
the primary site; needs general state channel / 67 spans to also catch the DTO). (3) Cold-start: first query ~24s
(embeds all nodes once), warm ~1-7s — the "~1s/query" holds only after the first.

Synthesis confirmed: hand VARD's ~720-token span list to the agent -> it reads the top few + reasons to pinpoint
& name missed state -> agent-precision at VARD-token-cost (few k tokens vs 24k alone).

## Run 13 — scaled head-to-head on ContextBench Java (n=5 verified, the runnable ones)

| instance | gold java | VARD gh@8 | agent | agent tokens |
|---|:--:|:--:|:--:|:--:|
| fastjson2 parseObject | 1 | 1/1 | 1/1 | 20,354 |
| fastjson2 TypeUtils | 1 | 0/1 | 1/1 | 43,537 |
| jackson canonicalize | 4 | 2/4 | 1/4 | 38,293 |
| jackson constraints | 1 | 1/1 | 1/1 | 22,919 |
| mockito enforcer | 9 | 5/9 | 2/9 | 27,871 |
| AGGREGATE | 16 | **9/16 (56%)** | **6/16 (37%)** | 152,974 total |

VARD general_hybrid@8: 56% file recall, 0 LLM tokens, ~3.5s/query, ~800-tok context.
Agent: 37% file recall, 152,974 LLM tokens (~30k/instance), ~80s/instance.

Why VARD out-recalls: agent UNDER-localizes multi-file bugs (names obvious 2-3, misses subtle ones — mockito got
enforcer+core, missed CreationSettings/MockNameImpl/plugin-registry); VARD's ranked span list covers more (2/4,
5/9). Agent is precise; VARD is broader+free. Agent won TypeUtils (issue named it, agent grepped straight to it).

Caveats: n=5 (10/15 java-verified failed checkout — unreachable SHAs); ContextBench issues are PR descriptions
that NAME the target API -> easy localization, not dark; VARD's recall edge is breadth (8 spans vs agent's 2-3
precise files) — agent wins on PRECISION; agent arm can't scale (153k tokens / 7min for 5) while VARD is free.

## Run 14 — full BUG-FIX head-to-head with the PACKAGED vard (honest counter-result)

youlai-boot current-user bug. Both arms = subagents that localize AND specify the fix. Arm A: plain (own
search). Arm B: given packaged-vard context (`vard context` + `vard_state_lineage('CurrentUserDTO')`, ~1145 tok).

| metric | Arm A plain | Arm B +VARD |
|---|:--:|:--:|
| fix correct vs gold | yes (both gold files) | yes (both gold files; roleNames as Set = exact gold) |
| LLM tokens | 28,554 | 32,281 |
| tool calls | 12 | 15 |
| time | 85s | 109s |

VARD did NOT save tokens here — cost slightly MORE. Honest reason: on a FINDABLE bug in a SMALL repo the agent
localizes cheaply itself, so VARD's context is additive (Arm B read it AND still verified by reading the mapper /
getUserProfile), not a substitute. A cautious agent re-verifies regardless. The token win only materializes if the
agent TRUSTS the spans and skips its own search — which it didn't.
Where VARD's token win IS real: (1) localization-only — VARD localized this same bug at 0 LLM tokens / 7.6s vs the
agent's ~24k; (2) hard/dark bugs where the agent flails 100k+ tokens and still misses the textually-disconnected
producer (the 0/7 -> found cases). This bug was outside that sweet spot.
TAKEAWAY: VARD's value = localization cost (0 tokens) + recall on bugs the agent can't find; NOT end-to-end fix
tokens on easy findable bugs (agent reads the code anyway).

## Run 15 — DOES THE DB EARN ITS EXISTENCE? whole_picture vs agent+git (the decisive test)

Gave an agent the same repo + FULL git access (git log/blame allowed) and asked for the same whole picture
(coupling / key decision / co-changes) for UserServiceImpl. vs vard_whole_picture (0 LLM tokens).

| dimension | vard_whole_picture (0 tok) | agent+git (48,037 tok, 9 tools, 54s) |
|---|---|---|
| coupling | cache:cacheKey -> 3 files (vague) | found the REAL auth:user:token_version key + mechanism + dismissed false positives (captcha) — MORE precise |
| decision | faf6754 only | found 289f79c (JWT tokenVersion, VARD MISSED) + faf6754 — RICHER |
| co-changes | raw counts | same + reasoned |

VERDICT: for git/code-reconstructable context, the agent MATCHED-OR-BEAT the DB on capability (deeper, caught what
VARD missed); the DB won ONLY on cost/latency (0 vs 48k tokens, instant vs 54s). Cost-efficiency alone is a weak
moat vs a "good enough" improving model. CRUCIAL: everything tested was IN the repo+git = the agent's home turf;
the test had nothing the agent couldn't reach. => the git-mined DB competes on the agent's turf and loses on
capability. The DB earns its existence ONLY on NON-RECONSTRUCTABLE data: business rules, off-repo decisions, Jira
discussions, incident postmortems, tribal knowledge — which the agent CANNOT get at any token cost because it is
not in the repo. THAT is the only thing that beats "agent + git is good enough" => the WRITE path is the product,
not the mined read path. Next fair test: put a real non-reconstructable rule/decision in the DB and show the
agent cannot get it on its own.

## Run 16 — RWR vs bipartite-inverted-index vs current, validated on git co-change (eval/coupling_compare.py)

Both ideas consume VARD's EXISTING resource edges (tests retrieval, not edge construction). Ground truth =
git co-change (held-out validation; confounded proxy). n=11 (youlai) / 38 (KCloud) targets, 2 repos.

| method | KCloud r@10 | youlai r@10 | coverage |
|---|:--:|:--:|:--:|
| baseline (current, shared-resource count) | 0.091 | 0.149 | 0.09-0.15 |
| bipartite (idf x mode x mutability, noisy-OR) | 0.091 | 0.149 | 0.09-0.15 |
| RWR (projected weighted graph + restart) | 0.269 | 0.673 | 1.0 |
| RANDOM (rank the resource-touching pool) | 0.267 | 0.758 | 1.0 |

VERDICT (both ideas LOSE on current edges):
- RWR ≈ RANDOM (KCloud 0.269 vs 0.267 — identical; youlai random higher). Its recall over baseline is pure
  VOLUME (gold ⊆ the small resource-touching pool; top-k covers it by size), not ranking signal. Multi-hop adds
  no real coupling signal here — confirms the user's own idea-2 analysis ("multi-hop goes noisy; graph solves a
  problem you don't have").
- bipartite == baseline EXACTLY: reweighting is moot when the 1-hop candidate set per file is tiny (few direct
  sharers) — nothing to re-rank. Needs DENSE edges to matter.
- THE REAL BOTTLENECK = edge construction: coverage 0.09-0.15 means detected resource-coupling explains only
  ~10% of co-change; the other ~90% has no detected edge. Swapping retrieval (current->bipartite->RWR) moves
  nothing. Exactly the user's idea-2 conclusion: "RWR sits downstream of edge construction; the win is the edge
  signal (match_confidence / key-resolution via AST/dataflow), not RWR-vs-BFS."
- Positive: current coupling is HIGH-PRECISION/LOW-RECALL (MRR ~0.97-0.99) — when it fires, the partner is #1-2.
Caveats: co-change confounded (caps recall for ALL methods); verdict conditional on current edge sparsity (with
denser/better edges, re-test — bipartite/RWR could then differentiate); small n. No vard/ code modified.

## Run 17 — rethink node/edge CONSTRUCTION: types/entities as shared-state resources (eval/edges2.py)

Generalized "resource" from cache/queue/table to DOMAIN TYPES (a type produced by one file, referenced
by another = coupling; IDF over file-fanout = native hub correction). Re-ran the co-change harness.

| repo | model | baseline r@10 | random r@10 | coverage |
|---|---|:--:|:--:|:--:|
| youlai (263f) | v1 cache/queue/table | 0.149 | 0.759 | 0.15 |
| youlai | v2 +types | 0.129 | **0.066** | 0.21 |
| youlai | v2-tight (data-only, <=15 fanout) | 0.114 | 0.073 | 0.15 |
| KCloud (1998f) | v1 | 0.091 | 0.265 | 0.09 |
| KCloud | v2 +types | 0.008 | 0.009 | 0.05 |
| KCloud | v2-tight | 0.012 | 0.018 | 0.01 |

VERDICT: construction was the lever (confirms Run 16). On a MODERATE repo (youlai), adding type/entity
resources flips baseline/bipartite from LOSING to random (v1) to BEATING random ~2x (v2) with higher
coverage + MRR 0.92-0.94 = real coupling signal where v1 had none. But it does NOT scale: on a large
monorepo (KCloud) type-coupling OVER-CONNECTS (1117 of 1998 files) and dilutes to ~random even tightened;
file-level "shared type" is too coarse there. Persistent: bipartite==baseline (weighting still doesn't
separate — retrieval is second-order; CONSTRUCTION moved the needle); MRR high everywhere (top-1 precision
is fine, recall/coverage is the gap; co-change is a confounded proxy capping recall for ALL methods).
NEXT LEVERS (unsolved at scale): producer<->consumer-only edges (drop read-read = the over-connection
source), FIELD/COLUMN-level resolution (couple on the mutated field not the whole entity), cache-key
TEMPLATE resolution (match_confidence). No vard/ code modified — eval/coupling_compare.py + eval/edges2.py.

## Methodology guardrails (must hold whenever VARD-vs-agent numbers are written up)
- Any VARD-vs-agent run on the CURRENT indexed repo is HISTORY-LEAKAGE-INFLATED: VARD's history/state
  signals have seen those commits. The ONLY trustworthy numbers are from the dark-gold / held-out harness
  indexed at the PARENT commit. Report those; treat retro-runs on master as upper bounds, not results.
- Per Run 8: coupling-class bug COMMITS are rare in public history, so bug-recovery evidence is capped at
  n≈2 strong + mechanism validation. State that ceiling wherever results are reported.

## Next
1. Precision v2: select only query-IMPLICATED resources (not all of a kind); producer-only refs (return-type==T
   convertors) instead of all referencers. Target: closure back toward the hand-built ~18.
2. Scale: expand curated set to ~15 bugs (more KCloud domain fixes + other repos/patterns) for a stable rate.
3. Build bugs were caught + fixed during this run: gold-symbol `<module>` filtering, structural-channel over-reach,
   return-type window, infra-type domination, primary-vs-secondary selection. All in git-free eval/ (local).
