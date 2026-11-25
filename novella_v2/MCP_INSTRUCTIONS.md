# ChatNovella V2

This server handles only new NV2_ sessions. Use the existing Lite plugin for
LITE_ sessions. Never migrate or rewrite Lite/CN_ games through this server.

At the first use in a new chat, call load_v2_instructions once and read the
complete rules.content and skin_builder.content, verifying their UTF-8 SHA-256.
Thereafter use those versions for the current chat. Reload only when the user
explicitly requests an update (or once to recover texts lost after compaction).
instruction_versions returns hashes only. A refresh is technical, not a turn.
Never rewrite, replace or supplement the author's Skin Builder. If empty, keep
it empty. Rules describe mechanics; they must not dictate literary style.

For new setup, preserve the entire author's original verbatim. Assign all text
exactly once with ordered unique anchors or contiguous Unicode ranges. Preserve
English character IDs. Supply directed familiarity and only canon-supported
initial relationships/threads. Prepare kind=canon and read all chunks. Show the
complete proposed questionnaire and obtain explicit approval of canon_hash
before approve_v2_canon, opening, or any gameplay. A deployment acceptance test
may approve its own clearly isolated synthetic test fixture, never a real game.

STORY DIRECTOR / PERSISTENT HOOKS (MANDATORY):
Before choosing participants or calling prepare_v2_context for a NEW game
turn, first call get_v2_director_brief(session_id). Read EVERY open arc and
EVERY individual unresolved hook, promise, mystery, consequence, NPC goal,
genre thread and explicit author appearance window. This read-only brief
does NOT move time, mutate the world, or grant facts to POV/NPC. The source
of an author deadline is the original canon, never an invented timer or
rotation scheme. Author-specified arrival windows take precedence over the
generic no-NPC-schedule guidance. If an authored deadline is already missed,
report it; NEVER retroactively invent or rewrite scenes to pretend compliance.

Use the brief + reader's input + current scene to decide participants; load
FULL character cards for every genuinely participating NPC with prepare.
For entering characters not already prepared, reprepare once and read the
new packet. A director-only actor who acts OFFSCREEN is not automatically
POV knowledge, and cannot receive personal knowledge without validated
scene access.

Each open question must retain its own stable ID in hooks.md; open_threads.md
is the concise arc index, NOT a replacement for persistent mystery details.
Use hook_updates in save_v2_turn only when a hook was truly introduced,
progressed, blocked or resolved, with a literal quote from the FINAL scene.
Updating one hook must NEVER erase any sibling hook or NPC objective.
Offscreen events must actually be narrated to count; do not invent unseen
progress and claim it happened.

For EXISTING sessions, hooks.md may legitimately be missing; do not wipe
or automatically replace old history. When the reader explicitly requests
plot-memory recovery, inspect original canon and actual saved history, then
call backfill_v2_hooks with exact source snippets and origin_turn=0 for canon
or the actual saved scene number. Never fabricate evidence, invent
deadlines, change original canon, or use the still-rollbackable last turn.
This action changes session version; reprepare afterward.

At the 15/60 audit, when hooks exist, supply report.story mapping EVERY
open hook ID to a specific note under 'hook:<id>' and EVERY unresolved
arc under 'arc:<id>', including not-started lines, overdue author
appearances, stagnating NPC goals and genre strands. Do not force an
event merely because the audit flags a long gap. Check independent quote
provenance, and preserve existing user choices and storyline pace.

For each new turn choose one stable request_id. Call prepare_v2_context with
that ID, exact reader_text and real participants. If already_committed_duplicate
is true, return its saved narration without generating or saving another turn.
If reused_pending_packet is true, continue that packet and read ONLY its
unread_chunk_indices. Retain session_id, request_id, packet_id and read progress.
Use read_v2_chunk to read each needed index once; verify each chunk's UTF-8
SHA-256 against the manifest and the joined packet against its total SHA-256.
Read all chunks before writing; require all_chunks_read=true from the last
chunk response. Do not call prepare again merely to check readiness.

relationships.md and open_threads.md must be read in EVERY turn/audit/canon
packet, including empty files. Rules and Skin Builder are not in these packets.
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

MANDATORY FULL SKIN BUILDER SCENE PREFLIGHT BEFORE EVERY SAVE:
Draft the complete narration without publishing it. Compare that entire draft
against EVERY line, requirement and prohibition of the currently loaded,
authoritative Skin Builder. Do not substitute prior scenes, recent messages,
a shortened summary or a generic scene-quality checklist for the author's text.
Check header field order, all message/SMS visual formatting, counts/length,
dialogue and input preservation, active POV, NPC/world autonomy, character
cards and knowledge boundaries, genre execution, director continuity, ending,
suggested choices, state/relationships output and ALL other authored rules.
Check EVERY applicable instruction within EACH heading, not just the heading.

After loading instructions, call get_v2_review_manifest ONCE per chat
(or after an explicit instructions refresh) to obtain the COMPLETE list
of substantive authored rule lines with stable IDs. Never call it every turn.
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

Commit using the ORIGINAL request_id, packet_id, chunk_hashes and reader_text.
Publish narration ONLY after saved=true; return the confirmed saved narration.
Identical retries must preserve the entire payload and must not create a turn.
On timeout/5xx retry that exact operation at most twice. If save status is
unknown, prepare with the SAME request_id to recover a committed result.
For 4xx/validation errors read the exact code and correct the cause once; if it
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
