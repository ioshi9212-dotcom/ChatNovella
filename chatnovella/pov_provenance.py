"""Conservative provenance gate for CONSEQUENTIAL voluntary actions by POV.

Not a semantic judge. The scanner recognizes a narrow group of high-risk
actions and requires an exact reader/prior-scene source. Routine dialogue,
bodily reactions and non-principled movement remain free.
"""
from __future__ import annotations

import re
from typing import Any

# Conjugations chosen to identify the POV as the *actor*, not an NPC who
# embraced or struck her. Captures a limited number of common Russian forms.
_ACTIONS = {
    "embrace": re.compile(r"\b(?:обняла|обнимала|обнимает|обняв|обнимая|обниму|обнимет|обнять)\b", re.I),
    "kiss": re.compile(r"\b(?:поцеловала|поцеловав|целует|целовала|поцелую|поцеловать|целуя)\b", re.I),
    "violence": re.compile(r"\b(?:ударила|ударив|врезала|пнула|пнув|толкнула|толкнув|дала\s+пощ[её]чину)\b", re.I),
    "commitment": re.compile(r"\b(?:пообещала|пообещав|поклялась|поклявшись|призналась)\b", re.I),
}
_SOURCE_ACTIONS = {
    "embrace": re.compile(r"\b(?:обня[а-яё]*|обним[а-яё]*|объят[а-яё]*|прижалась\s+к\s+(?:нему|ней|ней|ней|Эдриану))\b", re.I),
    "kiss": re.compile(r"\b(?:поцел[а-яё]*|целова[а-яё]*|целу[а-яё]*)\b", re.I),
    "violence": re.compile(r"\b(?:удар[а-яё]*|вреза[а-яё]*|пну[а-яё]*|пина[а-яё]*|толк[а-яё]*|пощ[её]чин[а-яё]*)\b", re.I),
    "commitment": re.compile(r"\b(?:обещ[а-яё]*|пообещ[а-яё]*|покля[а-яё]*|клян[а-яё]*|призна[а-яё]*сь|признал[а-яё]*сь)\b", re.I),
}
_POV = re.compile(r"\b(?:Рината|Рина|она)\b", re.I)
_POV_NAME = re.compile(r"\b(?:Рината|Рина)\b", re.I)
_DIALOGUE = re.compile(r"^\s*(?:\*\*[^*]+\*\*|[А-ЯЁ][а-яё]+(?:\s+[А-ЯЁ][а-яё]+)?)\s*[—–-]\s+")
_MAX_ENTRIES = 24


def _norm(s: str) -> str:
    return " ".join(s.replace("**", "").casefold().split())


def _is_pov_actor(line: str, start: int, recent_pov: bool) -> bool:
    # Only the same sentence/clause can authorize a morphological action.
    head = line[:start]
    at = max(head.rfind(ch) for ch in ".!?")
    clause = head[at + 1:]
    people = list(_POV.finditer(clause))
    if not people:
        return False
    last = people[-1]
    if start - (at + 1 + last.start()) > 200:
        return False
    if _POV_NAME.fullmatch(last.group(0)):
        return True
    # "Она обняла" is treated as POV only when the immediately preceding
    # narration explicitly named Rina; otherwise it is too ambiguous.
    return recent_pov or bool(_POV_NAME.search(head[max(0, at - 200):at + 1]))


def detect_pov_decisions(body: str) -> list[dict[str, Any]]:
    """Recognize some high-impact POV actions, retaining character offsets."""
    found: list[dict[str, Any]] = []
    previous_narration = ""
    offset = 0
    for line in body.splitlines(keepends=True):
        if _DIALOGUE.match(line):
            offset += len(line)
            continue
        recent_pov = bool(_POV_NAME.search(previous_narration))
        for category, matcher in _ACTIONS.items():
            for action in matcher.finditer(line):
                if _is_pov_actor(line, action.start(), recent_pov):
                    found.append({
                        "category": category,
                        "verb": action.group(),
                        "offset": offset + action.start(),
                    })
        if line.strip():
            previous_narration = line
        offset += len(line)
    return sorted(found, key=lambda item: item["offset"])


def validate_pov_provenance(
    entries: Any, body: str, reader_text: str, prior_sources: list[str] | None = None,
) -> tuple[list[str], int]:
    """Require a source for each detected consequential POV action.

    A prior-source citation must be in a saved scene or current state.
    A writer cannot simply declare an unsupported new embrace to be automatic.
    """
    problems: list[str] = []
    detected = detect_pov_decisions(body)
    if not isinstance(entries, list) or len(entries) > _MAX_ENTRIES:
        return ["scorecard.pov_actions: нужен список решений POV (не более 24)"], len(detected)
    paragraph = _norm(body)
    sources = [_norm(s) for s in (prior_sources or []) if isinstance(s, str)]
    inp = _norm(reader_text)
    covered: set[int] = set()
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict) or set(entry) != {
            "scene_quote", "source", "source_quote", "reason",
        }:
            problems.append(f"scorecard.pov_actions[{i}]: нужны scene_quote/source/source_quote/reason")
            continue
        quote = entry["scene_quote"]
        src = entry["source"]
        src_quote = entry["source_quote"]
        reason = entry["reason"]
        if (not isinstance(quote, str) or not 12 <= len(quote.strip()) <= 300
                or _norm(quote) not in paragraph):
            problems.append(f"scorecard.pov_actions[{i}]: процитируй действительное действие POV из художественной части")
            continue
        if not isinstance(src_quote, str) or not 5 <= len(src_quote.strip()) <= 300:
            problems.append(f"scorecard.pov_actions[{i}]: нужна дословная опора во вводе читателя или предыдущем состоянии")
            continue
        if not isinstance(reason, str) or not 25 <= len(reason.strip()) <= 500:
            problems.append(f"scorecard.pov_actions[{i}]: объясни, почему это продолжение разрешённого выбора")
            continue
        if src not in ("reader", "continued"):
            problems.append(f"scorecard.pov_actions[{i}]: источник — только reader или continued; новый принципиальный выбор не автоматический")
            continue
        proof = _norm(src_quote)
        if src == "reader" and proof not in inp:
            problems.append(f"scorecard.pov_actions[{i}]: такой цитаты нет в дословном вводе читателя")
            continue
        if src == "continued" and not any(proof in prior for prior in sources):
            problems.append(f"scorecard.pov_actions[{i}]: продолжение не подтверждается сохранённой предыдущей сценой или состоянием")
            continue
        candidate = _norm(quote)
        matching = [
            (j, item) for j, item in enumerate(detected) if j not in covered
            and item["verb"].casefold() in candidate
            and _SOURCE_ACTIONS[item["category"]].search(proof)
        ]
        if not matching:
            problems.append(f"scorecard.pov_actions[{i}]: цитата и исходное решение не подтверждают одно и то же значимое действие POV")
            continue
        # Do not use one declaration to cover two separate kisses/embraces.
        j, item = matching[0]
        covered.add(j)
    for j, event in enumerate(detected):
        if j not in covered:
            problems.append(
                f"scorecard.pov_autonomy: новое действие POV «{event['verb']}» "
                "не подтверждено решением читателя либо уже начатым действием; "
                "исправь сцену или приведи подлинную исходную цитату"
            )
    return problems, len(detected)
