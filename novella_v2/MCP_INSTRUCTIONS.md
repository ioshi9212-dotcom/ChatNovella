# ChatNovella V2

This server handles only new NV2_ sessions. Use the existing Lite plugin for
LITE_ sessions. Never migrate or rewrite Lite/CN_ games through this server.

At the first use in a new chat, call load_v2_instructions(rules_only=true)
and read the complete Rules before the director. Do not fetch Skin Builder
at chat bootstrap. Each new turn packet supplies current Rules first,
then the last three full active scenes in chronological order, then all
world context and the current full Skin Builder in dedicated FINAL chunks.
Never rewrite, replace or supplement the author's Skin Builder. If empty,
keep it empty. Rules describe mechanics; Skin Builder governs artistic style.
The full combined instructions endpoint is only for explicit inspection.
instruction_versions returns hashes only; refresh is not a game turn.

For new setup, preserve the entire author's original verbatim. Assign all text
exactly once with ordered unique anchors or contiguous Unicode ranges. Preserve
English character IDs. Supply directed familiarity and only canon-supported
initial relationships/threads AND every canon-supported initial_hooks item
with its exact author quote, stable arc_id, independent ID and explicit
window_start/window_end ONLY when stated literally in the source. Prepare
kind=canon and read all chunks. Show the
complete proposed questionnaire and obtain explicit approval of canon_hash
before approve_v2_canon, opening, or any gameplay. A deployment acceptance test
may approve its own clearly isolated synthetic test fixture, never a real game.

STORY DIRECTOR / PERSISTENT HOOKS (MANDATORY):
Before choosing participants or calling prepare_v2_context for a NEW game
turn, first call get_v2_director_brief(session_id). Read EVERY open arc and
EVERY individual unresolved hook, promise, mystery, consequence, NPC goal,
genre thread and explicit author appearance window. The server records a
version-bound receipt and rejects prepare without it, even for an empty ledger.
This brief
does NOT move time, mutate the world, or grant facts to POV/NPC. The source
of an author deadline is the original canon, never an invented timer or
rotation scheme. Author-specified arrival windows take precedence over the
generic no-NPC-schedule guidance. If an authored deadline is already missed,
report it; NEVER retroactively invent or rewrite scenes to pretend compliance.

Use the brief + reader's input + current scene to decide participants; load
FULL character cards for every genuinely participating NPC with prepare.
For existing entering characters not already prepared, reprepare once and read
the new packet. For genuinely new NPCs, pass new_characters to prepare:
{name, display, established_text} (stable English ID, name, planned literal
scene excerpt). add_v2_character is validation only; it returns a proposal,
not a registered character. Proposals live in the current packet only,
not in canon, state or knowledge files. If the final draft changes the
excerpt, pass the exact final quote in save_v2_turn.new_character_evidence
(mapping staged NPC ID to quote). No reprepare solely for a changed quote
if the NPC identity and participants are unchanged. Only
final narration with the exact excerpt and actual participation
registers a new NPC in the same commit as all game changes. Unused proposals
are discarded. Rollback of that turn removes its new NPC documents and cast
entries; original canon and cancelled archive narration stay intact. A director-only actor who acts OFFSCREEN is not automatically
POV knowledge, and cannot receive personal knowledge without validated
scene access.

Each open question must retain its own stable ID in hooks.md; open_threads.md
is the concise arc index, NOT a replacement for persistent mystery details.
Use hook_updates in save_v2_turn only when a hook was truly introduced,
progressed, blocked or resolved, with a literal quote from the FINAL scene.
Updating one hook must NEVER erase any sibling hook or NPC objective.
A closed hook remains stored with its specific outcome and exact evidence.
A literal quote alone does NOT prove the stated outcome; audit its meaning.
Do not return resolved obligations to the overdue list. Track each timed NPC
appearance separately. Recorded participation is evidence to inspect, not
a guarantee of the intended debut.
Offscreen events must actually be narrated to count; do not invent unseen
progress and claim it happened.

The director brief also extracts literal author timing clauses from
original.txt as author_window_candidates even when an older session has no
hooks.md yet. These are SOURCE-QUOTED HINTS, not automatically persisted
facts or mandatory instant appearances. Review elapsed untracked author windows against first recorded participation
and saved scenes; elapsed does not mean violated. Use historical backfill
when asked. A blocked hook with a future in-world condition is not abandoned.

For EXISTING sessions, hooks.md may legitimately be missing; do not wipe
or automatically replace old history. When the reader explicitly requests
plot-memory recovery, inspect original canon and actual saved history, then
call backfill_v2_hooks with exact source snippets and origin_turn=0 for canon
or the actual saved scene number. Provide independent last_turn and last_evidence
for later confirmed development. Historical fulfillment uses status=closed and
actual completion evidence. For an existing open hook use reconcile=true;
for a mistaken closure use reopen=true with the original identity and evidence.
Never fabricate evidence, invent deadlines, change original canon, or cite the
still-rollbackable last turn. Backfill survives a later rollback. This action
changes session version; call director again and reprepare afterward.

At the 15/60 audit, report.story must cover EXACTLY audit_story_ids from
the packet: every live hook, open arc, author window, current consequences
and a rotating sample of old closures at 60. Server checks all ledger
records and archive evidence at every audit. Do not claim semantic proof
from a literal quote alone. Preserve user choices and storyline pace.

For each new turn choose one stable request_id. After the director brief,
provide story_plan to prepare_v2_context: focus_ids (known arc:/hook:/window:
IDs, may be empty), direction, world, deferred (short concrete reasoning),
priority (map every plan_priority_ids ID to an actual decision or grounded
reason to defer). Required when plot records/windows exist; recommended even
when empty. Plan is not an event; never force an encounter or progress quota.
Use trigger date:YYYY-MM-DD, turn:N, hook:ID for known waiting conditions,
otherwise text. Due does not mean forced. Closed results remain in
resolved_hooks of the director brief and resolved_outcomes of the turn packet. Record actual consequences as separate hooks. Every open
hook needs an existing open arc; repair missing arcs with explicit historical
arc_repairs (id,title,status,note,source,origin_turn) in backfill_v2_hooks.
Call prepare_v2_context with that ID, exact reader_text and real participants. If already_committed_duplicate
is true, return its saved narration without generating or saving another turn.
If reused_pending_packet is true, continue that packet and read ONLY its
unread_chunk_indices. Retain session_id, request_id, packet_id and read progress.
Use read_v2_chunk to read each needed index once; verify each chunk's UTF-8
SHA-256 against the manifest and the joined packet against its total SHA-256.
Read every chunk in ascending order, starting at 0, following next_chunk_index.
Rules precede recent_scenes (oldest to newest), then files and plan. The
Skin Builder block starts at skin_chunk_start and is always last. Read the
complete final block, including all its chunks, before writing. Require
all_chunks_read=true. Do not call prepare merely to check readiness.

relationships.md and open_threads.md must be read in EVERY turn/audit/canon
packet, including empty files. Rules starts every packet. Skin Builder is
last in turn packets only; audit and canon do not contain it.
Load complete cards and personal knowledge for actual scene participants.
Director context, hidden lore, relations, arcs, timeline and other characters'
memories do not automatically become an NPC's knowledge. Before committing,
check each speaker's factual claims once against their actual access. Add only
new witnessed facts to the correct owner with a literal narration quote.

Use the exact eight state fields: turn, datetime, location, pov, present, remote,
objects, unfinished. Each physical actor has position, clothing, footwear,
items, physical, unfinished. Keep biography, memory and feelings out of state.
datetime may be null; do not invent a date. An actor who left before the final
state needs scene_access with an exact quote proving physical/remote/ability
access during this turn. Loading a card alone proves no participation.

Save only REAL directed relationship changes. Missing familiarity means
unaware; aware and acquainted are independent in each direction. Acquaintance
does not expose secrets. Keep the author's metric names and meanings. For
familiarity use a reason grounded in the scene (include a short exact quote);
for feelings include exact evidence. NPC→POV values are 0..100, usually ±1..3;
a larger change to an existing value requires significant=true, reason and
evidence. NPC→NPC links are short words, metric=null. If no change, relations=[].
Never change the embedded relationship rule during opening or audit.

Check open arcs once after the scene. thread_updates includes only new,
substantially changed or actually closed threads, with stable id and an exact
source quote from narration. Otherwise use []. Closed threads leave the
working file but remain archived. No forced deadlines or scheduled revelations.

SCENE REVIEW MODE (server-controlled; disabled by default):
Read scene_review_enabled from prepare and the final Skin Builder block.
If false, DO NOT call review_v2_scene or get_v2_review_manifest, create
review reports, enforce separate machine literary format, or loop rewriting.
Write the scene directly according to the fresh full Skin Builder, including
its own final-output instructions. Then compute game changes from the final
narration and call save_v2_turn. Technical integrity checks remain mandatory.
If true, use the retained strict preflight procedure below.

STRICT PREFLIGHT — ONLY WHEN scene_review_enabled=true:
Draft the complete narration without publishing it. Compare that entire draft
against EVERY line, requirement and prohibition of the fresh, last-read,
authoritative Skin Builder. Do not substitute prior scenes, recent messages,
a shortened summary or a generic scene-quality checklist for the author's text.
Check header field order, all message/SMS visual formatting, counts/length,
dialogue and input preservation, active POV, NPC/world autonomy, character
cards and knowledge boundaries, genre execution, director continuity, ending,
suggested choices, state/relationships output and ALL other authored rules.
Check EVERY applicable instruction within EACH heading, not just the heading.

The final Skin Builder block contains review_sections, review_rule_ids
(stable L-numbered lines) and the full content, in that order. Use them
directly; no separate manifest call during normal turn loading.
For EACH section submit ReviewCheck {section, result, note}. In addition,
for EVERY authored line from that manifest submit RuleCheck
{id, result: pass | not_applicable | fail, note: specific explanation,
evidence: literal excerpt from the FINAL narration if pass, otherwise null}.
A blanket section PASS is NOT proof of compliance: the server requires
all individual rules and verifies exact literal excerpts for PASS.
For SMS/messages visibly presented as sender and timestamp, the server
independently rejects ordinary unframed text; use a visibly separate chat
block (e.g. Markdown quote/bubble), never just name and time.
Use the actual Skin Builder to judge EVERY other requirement as written.
Call review_v2_scene with BOTH checks and rule_checks, request_id,
packet_id, expected_version, final narration and current skin_sha256. Server verifies the
complete heading set, mandatory machine-checkable layout, final narration
digest and current Skin Builder version; review_v2_scene returns approved.
Never call save_v2_turn without approved=true for the EXACT final text.
If any issue is found, repair the actual draft (local format fix or full
literary rewrite as necessary), then re-audit ALL sections and call review
again with the corrected complete narration. At most two corrected drafts;
if still failing do NOT save/publish a defective scene. Explain failure.
Recompute any scene-dependent knowledge, relationship, timeline, arc and
witness evidence from the final approved narration, not from discarded drafts.
The server cannot verify artistic meaning; the model must make the review
specific and faithful to the author's ENTIRE Skin Builder. Never rewrite the
author's Skin Builder itself to satisfy the review.

Before successful commit, do NOT mutate any game documents, including NPC
cards, history, timeline, relationships, knowledge, hooks, arcs and world state.
If enabled, review writes only a technical receipt. Derive all updates from the
final narration. Commit using the ORIGINAL request_id, packet_id, chunk_hashes and reader_text.
Publish narration ONLY after saved=true; return the confirmed saved narration.
Identical retries must preserve the entire payload and must not create a turn.
On timeout/5xx retry that exact operation at most twice. If save status is
unknown, prepare with the SAME request_id to recover a committed result.
If RULES_VERSION_CHANGED, SKIN_VERSION_CHANGED or REVIEW_MODE_CHANGED, reprepare with the same
request_id, reader_text and plan, then read the new ordered packet and use
its fresh final Skin Builder. For other 4xx/validation errors read the exact code and correct the cause once; if it
recurs, stop and explain it. Never loop prepare/read/rewrite. A near-duplicate
warning is informational and does not require an artificial plot change.

At turn 15/60 the next turn is blocked until the required audit. Prepare audit,
read all chunks and verify hashes, then check the required report domains.
At 60 include long_term. Corrections need before_sha256, after and reason;
knowledge/timeline additions and relationship changes need exact witnessed
source evidence. No invented knowledge; never rewrite original/history.
Rollback only on explicit user request, after reading current status/history,
with the current expected_version and exact request_id of the last saved turn.
The archive must retain the cancelled scene. Search history using V2 tools only.
