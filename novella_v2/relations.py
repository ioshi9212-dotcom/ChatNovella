"""Compact directional relationships, without a second scene-memory system.

One JSON document stored in relationships.md; fixed-size status matrix for
the cast, sparse scores/descriptions, and immutable relationship rule per game.
"""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

STATUSES = {"unaware", "aware", "acquainted"}
MAX_SCALES = 10
MAX_NPC_DESCRIPTION = 240
RULE_TEMPLATE = Path(__file__).with_name("templates").joinpath("relationships.md")


class RelationProblem(ValueError):
    pass


def check(value: bool, msg: str) -> None:
    if not value:
        raise RelationProblem(msg)


def rule() -> str:
    return RULE_TEMPLATE.read_text(encoding="utf-8").strip()


def _word(value: Any, limit: int, field: str) -> str:
    check(isinstance(value, str) and
          0 < len(value.strip()) <= limit and
          "\n" not in value and "\r" not in value,
          field + " must be short nonblank single-line text")
    return value.strip()


def normalize(raw: dict, cast: set[str]) -> dict:
    """Validate and complete acquaintance rows; accept older two-key stores.

    Legacy explicitly saved links imply acquaintance; missing links never do.
    No guesses are made about relationships from author prose.
    """
    check(isinstance(raw, dict) and set(raw) in (
        {"npc_to_pov", "npc_to_npc"},
        {"rules", "familiarity", "npc_to_pov", "npc_to_npc"},
        {"familiarity", "npc_to_pov", "npc_to_npc"},
    ), "Relationship document requires familiarity, NPC→POV and NPC→NPC")
    metrics = deepcopy(raw["npc_to_pov"])
    npc_links = deepcopy(raw["npc_to_npc"])
    check(isinstance(metrics, dict) and isinstance(npc_links, dict),
          "Relationship sections must be maps")
    check(set(metrics) <= cast and set(npc_links) <= cast, "Unknown NPC relation owner")
    for src, row in metrics.items():
        check(isinstance(row, dict) and len(row) <= MAX_SCALES,
              "NPC→POV permits at most 10 personal scales")
        names: set[str] = set()
        for name, score in row.items():
            name = _word(name, 50, "Scale label")
            check(name.casefold() not in names, "Duplicate scale label")
            names.add(name.casefold())
            check(type(score) is int and 0 <= score <= 100,
                  "NPC→POV scale must be 0..100")
    for src, row in npc_links.items():
        check(isinstance(row, dict) and set(row) <= cast - {src},
              "NPC→NPC links must be directional toward other known cast IDs")
        for dst, text in row.items():
            _word(text, MAX_NPC_DESCRIPTION, "NPC→NPC current relation")

    people = cast | {"pov"}
    supplied = raw.get("familiarity", {})
    check(isinstance(supplied, dict) and set(supplied) <= people,
          "Unknown familiarity source")
    statuses = {}
    for src in sorted(people):
        given = supplied.get(src, {})
        check(isinstance(given, dict) and set(given) <= people - {src},
              "Unknown familiarity target")
        statuses[src] = {}
        for dst in sorted(people - {src}):
            explicit = given.get(dst)
            if explicit is not None:
                check(explicit in STATUSES, "Unknown familiarity status")
                statuses[src][dst] = explicit
            elif src in cast and ((dst == "pov" and metrics.get(src)) or
                                  (dst != "pov" and npc_links.get(src, {}).get(dst))):
                statuses[src][dst] = "acquainted"
            else:
                statuses[src][dst] = "unaware"

    for src, row in metrics.items():
        check(statuses[src]["pov"] == "acquainted" or not row,
              "Unacquainted NPC cannot have numeric feelings toward POV")
    for src, row in npc_links.items():
        for dst in row:
            check(statuses[src][dst] == "acquainted",
                  "Unacquainted NPC cannot have a directional relationship")

    chosen_rule = raw.get("rules", rule())
    check(isinstance(chosen_rule, str) and
          20 <= len(chosen_rule) <= 1200 and
          "\r" not in chosen_rule and "\n" not in chosen_rule,
          "Relationship rule must be one short immutable paragraph")
    return {"rules": chosen_rule,
            "familiarity": statuses,
            "npc_to_pov": metrics,
            "npc_to_npc": npc_links}


def update(existing: dict, updates: list[dict], cast: set[str],
           participants: set[str]) -> dict:
    """Apply only real changes. First acquaintance and initial metric are distinct.

    Runtime does not infer personal acquaintance, romance or other feelings.
    Reasons are stored in scene history, not appended to the working file.
    """
    current = normalize(existing, cast)
    check(isinstance(updates, list) and len(updates) <= 30,
          "Too many relationship changes in one turn")
    seen: set[tuple[str, str, str]] = set()
    changes = []
    for item in updates:
        check(isinstance(item, dict), "Relation change must be an object")
        identity = {"source", "target", "reason"}
        has_status = "status" in item
        if has_status:
            check(set(item) == identity | {"status"},
                  "Familiarity change needs source/target/status/reason")
        else:
            check(set(item) in (identity | {"metric", "value"},
                                identity | {"metric", "value", "significant"}),
                  "Feeling change needs source/target/metric/value/reason")
        src, dst = item["source"], item["target"]
        check(src in (cast | {"pov"}) and dst in (cast | {"pov"}) - {src},
              "Relationship change has unknown owner or target")
        # Participants are actors who truly took part in the scene, including
        # those connected by phone, reflection, another canon ability, etc.
        check((src == "pov" or src in participants) and
              (dst == "pov" or dst in participants),
              "Only participants who physically or remotely took part may change relations")
        _word(item["reason"], 180, "Relationship change reason")
        if has_status:
            check(item["status"] in STATUSES, "Unknown acquaintance status")
            key = (src, dst, "familiarity")
        else:
            check(src != "pov", "Only NPC feelings are stored in this file")
            if dst == "pov":
                key = (src, dst, _word(item["metric"], 50, "Scale label").casefold())
            else:
                check(item["metric"] is None, "NPC→NPC feelings use words, not scales")
                key = (src, dst, "description")
        check(key not in seen, "Same directed relation updated twice in one turn")
        seen.add(key)
        changes.append(item)

    # Status changes first, so a meeting and initial feelings can be written
    # in a single transaction regardless of change ordering.
    result = deepcopy(current)
    for item in changes:
        if "status" not in item:
            continue
        src, dst, new = item["source"], item["target"], item["status"]
        previous = result["familiarity"][src][dst]
        check(new != previous, "No-op familiarity update would bloat the archive")
        if previous == "acquainted" and new != "acquainted":
            check(item["reason"].strip(), "Acquaintance loss needs a reason")
        result["familiarity"][src][dst] = new
        if new != "acquainted" and src != "pov":
            if dst == "pov":
                result["npc_to_pov"].pop(src, None)
            elif src in result["npc_to_npc"]:
                result["npc_to_npc"][src].pop(dst, None)
                if not result["npc_to_npc"][src]:
                    del result["npc_to_npc"][src]

    for item in changes:
        if "status" in item:
            continue
        src, dst, value = item["source"], item["target"], item["value"]
        check(result["familiarity"][src][dst] == "acquainted",
              "Cannot record feelings about someone not personally acquainted")
        if dst == "pov":
            label = _word(item["metric"], 50, "Scale label")
            check(type(value) is int and 0 <= value <= 100,
                  "NPC→POV scales must be integers 0..100")
            row = result["npc_to_pov"].setdefault(src, {})
            if label in row:
                diff = value - row[label]
                check(diff != 0, "Unchanged metric must not be rewritten each turn")
                if abs(diff) > 3:
                    check(item.get("significant") is True and
                          len(item["reason"].strip()) >= 8,
                          "A jump larger than ±3 requires significant=true and an event reason")
            else:
                check(len(row) < MAX_SCALES,
                      "NPC→POV cannot exceed 10 individual scales")
                # First value is an initial baseline, not a change from zero.
                check(not ("significant" in item and
                           type(item["significant"]) is not bool),
                      "significant must be boolean")
            row[label] = value
        else:
            text = _word(value, MAX_NPC_DESCRIPTION, "NPC→NPC description")
            prior = result["npc_to_npc"].get(src, {}).get(dst)
            check(prior != text, "Unchanged NPC→NPC relation must not be rewritten")
            result["npc_to_npc"].setdefault(src, {})[dst] = text
    return normalize(result, cast)
