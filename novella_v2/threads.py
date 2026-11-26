"""Bounded author-grounded story threads, one short working Markdown file.

Only *unresolved* items live here. Changes and closures remain in full
immutable scene history. This is a cue for the writer, never a plot timer.
"""
from __future__ import annotations

import re

HEADER = "# Незавершённые линии\n"
MAX_OPEN = 512  # protective capacity, not a plot quota
ID = re.compile(r"^[a-z][a-z0-9_]{1,49}$")
ROW = re.compile(r"^- \[(не началась|активна)\] ([a-z][a-z0-9_]{1,49}) \| (.+) \| (.+) \| ход ([0-9]{1,8})$")
DISPLAY = {"not_started": "не началась", "active": "активна"}
PARSE_DISPLAY = {value: key for key, value in DISPLAY.items()}


class ThreadProblem(ValueError):
    """Bad file structure or invalid suggested thread change."""


def _check(condition: bool, message: str) -> None:
    if not condition:
        raise ThreadProblem(message)


def _short(value: object, limit: int, label: str) -> str:
    _check(isinstance(value, str), label + " must be text")
    clean = value.strip()
    _check(0 < len(clean) <= limit and "\n" not in clean and "\r" not in clean
           and " | " not in clean, label + " must be a short single line")
    return clean


def _entry(item: dict) -> dict:
    _check(isinstance(item, dict) and set(item) == {"id", "title", "status", "note", "turn"},
           "Thread has fixed id/title/status/note/turn fields")
    ident = item["id"]
    _check(isinstance(ident, str) and bool(ID.fullmatch(ident)),
           "Thread id must be stable lowercase English")
    _check(item["status"] in ("not_started", "active"), "Only unresolved threads stay here")
    _check(type(item["turn"]) is int and 0 <= item["turn"] < 100_000_000,
           "Invalid thread turn")
    return {"id": ident, "title": _short(item["title"], 120, "Thread title"),
            "status": item["status"], "note": _short(item["note"], 240, "Thread note"),
            "turn": item["turn"]}


def render(items: list[dict]) -> str:
    _check(isinstance(items, list) and len(items) <= MAX_OPEN,
           "Too many unresolved story threads: preserve entries and request maintenance")
    clean = [_entry(item) for item in items]
    _check(len({row["id"] for row in clean}) == len(clean),
           "Duplicate thread id")
    clean.sort(key=lambda v: (v["status"] != "active", -v["turn"], v["id"]))
    return HEADER + "".join(
        "- [" + DISPLAY[x["status"]] + "] " + x["id"] + " | " + x["title"] +
        " | " + x["note"] + " | ход " + str(x["turn"]) + "\n"
        for x in clean
    )


def parse(text: str) -> list[dict]:
    _check(isinstance(text, str) and text.startswith(HEADER),
           "Invalid open_threads.md heading")
    items: list[dict] = []
    for line in text[len(HEADER):].splitlines():
        match = ROW.fullmatch(line)
        _check(match is not None, "Invalid open_threads.md line")
        status, ident, title, note, turn = match.groups()
        items.append(_entry({"status": PARSE_DISPLAY[status], "id": ident, "title": title,
                             "note": note, "turn": int(turn)}))
    _check(render(items) == text, "Noncanonical or duplicated open_threads.md")
    return items


def seed(items: list[dict] | None, original: str) -> str:
    """Optional opening lines, each grounded in an exact author quotation."""
    _check(items is None or isinstance(items, list), "Initial threads must be a list")
    initial = []
    for item in items or []:
        _check(isinstance(item, dict) and set(item) ==
               {"id", "title", "status", "note", "source"},
               "Initial thread needs id/title/status/note/source")
        quote = _short(item["source"], 350, "Initial thread source")
        _check(quote in original, "Initial thread source must appear in original")
        initial.append(_entry({k: v for k, v in item.items() if k != "source"} |
                              {"turn": 0}))
    return render(initial)


def backfill(text: str, additions: list[dict], original: str,
             scenes: dict[int, str], current_turn: int) -> str:
    """Restore explicitly identified arcs from real historical evidence only."""
    current = {row["id"]: row for row in parse(text)}
    _check(isinstance(additions, list) and len(additions) <= MAX_OPEN,
           "Invalid historical arc repairs")
    seen = set()
    for item in additions:
        _check(isinstance(item, dict) and set(item) ==
               {"id", "title", "status", "note", "source", "origin_turn"},
               "Arc repair needs id/title/status/note/source/origin_turn")
        turn = item["origin_turn"]
        _check(type(turn) is int and 0 <= turn <= current_turn,
               "Invalid historical arc source turn")
        quote = _short(item["source"], 350, "Historical arc source")
        _check(len(quote) >= 8 and quote in (original if turn == 0 else scenes.get(turn, "")),
               "Historical arc needs exact original or saved-scene evidence")
        entry = _entry({k: v for k, v in item.items()
                        if k not in {"source", "origin_turn"}} | {"turn": turn})
        ident = entry["id"]
        _check(ident not in seen, "Duplicate historical arc repair")
        seen.add(ident)
        if ident in current:
            _check(current[ident]["title"] == entry["title"],
                   "Existing arc must preserve its title")
        else:
            current[ident] = entry
    return render(list(current.values()))


def apply(text: str, updates: list[dict], narration: str, turn: int) -> tuple[str, list[dict]]:
    """Update each ID once per scene; closed threads leave working file immediately."""
    current = {row["id"]: row for row in parse(text)}
    _check(isinstance(updates, list) and len(updates) <= 12,
           "Only actual changed threads may be updated in one scene")
    seen: set[str] = set()
    actual: list[dict] = []
    for item in updates:
        _check(isinstance(item, dict) and set(item) ==
               {"id", "title", "status", "note", "source"},
               "Thread update needs id/title/status/note/source")
        ident = item["id"]
        _check(isinstance(ident, str) and bool(ID.fullmatch(ident)), "Invalid thread id")
        _check(ident not in seen, "Same thread updated twice in one scene")
        seen.add(ident)
        title, note = _short(item["title"], 120, "Thread title"), _short(item["note"], 240, "Thread note")
        quote = _short(item["source"], 350, "Thread source")
        _check(len(quote) >= 3 and quote in narration,
               "Thread change needs exact excerpt from saved scene")
        status = item["status"]
        _check(status in ("not_started", "active", "closed"), "Invalid thread status")
        previous = current.get(ident)
        if previous is None:
            _check(status != "closed", "Cannot close a nonexistent thread")
        else:
            _check(previous["title"] == title,
                   "Existing thread must preserve its title")
            _check(not (previous["status"] == "active" and status == "not_started"),
                   "Started thread cannot become not started")
            _check(status == "closed" or
                   (previous["note"], previous["status"]) != (note, status),
                   "Do not rewrite unchanged thread each turn")
        if status == "closed":
            del current[ident]
        else:
            current[ident] = {"id": ident, "title": title, "status": status,
                              "note": note, "turn": turn}
        actual.append(dict(item))
    return render(list(current.values())), actual
