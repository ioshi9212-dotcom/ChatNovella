"""Deterministic part of the V2 scene review.

The literary judgement remains with the model reviewing EVERY part of the
author's current Skin Builder. This module checks objective formatting only.
It never treats past scenes as formatting examples or rewrites narration.
"""
from __future__ import annotations

import re

_HEADING = re.compile(r"^([А-ЯЁ][А-ЯЁA-Z0-9 /–—-]{3,65}):(?:\s.*)?$")
_RANGE = re.compile(r"(\d{3,5})\s*[-–—]\s*(\d{3,5})\s*символ", re.IGNORECASE)
_CHOICE = ("💙 Что я могу сделать:", "🔹 Что я могу сказать:",
           "🔸 Что я могу подумать:")


def is_formatted_builder(skin: str) -> bool:
    """An authored scene template, not arbitrary test instructions."""
    return "ФОРМАТ СЦЕНЫ" in skin and "📖" in skin


def review_sections(skin: str) -> list[str]:
    """Current authored sections; never a fixed stale copy of the builder."""
    if not skin.strip():
        return []
    result = ["ФОРМАТ СЦЕНЫ"] if skin.lstrip().startswith("ФОРМАТ СЦЕНЫ") else []
    for line in skin.splitlines():
        stripped = line.strip()
        found = _HEADING.match(stripped)
        numbered = re.match(r"^###\s*\d+\.\s*(.+?)\s*$", stripped)
        if found or numbered:
            label = (found or numbered).group(1).strip()
            if label not in result:
                result.append(label)
    return result or ["SKIN BUILDER: ВЕСЬ ТЕКСТ"]


def review_items(skin: str) -> list[dict]:
    """Every substantive authored line needs an individual review record.

    IDs are stable within an immutable SHA-pinned Skin Builder. Author text
    is preserved verbatim in the manifest; templates, examples and long
    multi-clause rules are NOT collapsed into vague section-level passes.
    """
    if not skin.strip():
        return []
    current = "ФОРМАТ СЦЕНЫ"
    result: list[dict] = []
    for n, raw in enumerate(skin.splitlines(), start=1):
        line = raw.strip()
        if not line or line in ("-", "***") or line.startswith("Версия "):
            continue
        numbered = re.match(r"^###\s*\d+\.\s*(.+?)\s*$", line)
        named = _HEADING.match(line)
        if numbered or named:
            current = (numbered or named).group(1).strip()
            continue
        # Descriptive fiction/legal preface is not an actionable scene rule.
        if line.startswith("{Данные правила применяются"):
            continue
        result.append({"id": f"L{n}", "section": current, "text": line})
    return result


def rule_report_errors(skin: str, narration: str, report: list[dict]) -> list[str]:
    """Fail closed on missing rules; require literal scene evidence for passes."""
    expected = {r["id"]: r for r in review_items(skin)}
    if not isinstance(report, list):
        return ["Отсутствует построчная проверка всех правил Skin Builder"]
    issues: list[str] = []
    seen: set[str] = set()
    for item in report:
        if not isinstance(item, dict) or set(item) != {
            "id", "result", "note", "evidence"
        }:
            issues.append("Проверка правила должна иметь id, result, note, evidence")
            continue
        key = item["id"]
        if not isinstance(key, str) or key in seen:
            issues.append("Дублированный или неверный идентификатор правила")
            continue
        seen.add(key)
        if key not in expected:
            issues.append("Проверен чужой или устаревший пункт: " + key)
            continue
        verdict, reason, evidence = item["result"], item["note"], item["evidence"]
        if verdict not in ("pass", "not_applicable", "fail"):
            issues.append("Некорректная оценка пункта " + key)
        if not isinstance(reason, str) or len(reason.strip()) < 18:
            issues.append("Нет конкретного объяснения пункта " + key)
        if verdict == "fail":
            issues.append("Неисправленное нарушение правила " + key + ": " +
                          str(reason)[:150])
        if verdict == "pass":
            if not isinstance(evidence, str) or len(evidence) < 8 or evidence not in narration:
                issues.append("Нет точной цитаты из сцены для проверки " + key)
        elif evidence is not None:
            issues.append("Для неприменимого пункта " + key + " evidence должен быть null")
    for key in expected:
        if key not in seen:
            issues.append("Не проверено правило Skin Builder: " + key +
                          " — " + expected[key]["text"][:100])
    return issues


# Strong high-confidence signs of a message being presented to the reader as
# sender + timestamp + text, rather than a character merely using a phone.
_SENDER_TIME = re.compile(
    r"^\s*(?:\*\*)?[А-ЯЁA-Z][\wА-ЯЁа-яё .-]{0,39}(?:\*\*)?"
    r"\s*(?:[,·—-]\s*)?[\[(]?(?:[01]?\d|2[0-3]):[0-5]\d[\])]?"
    r"\s*[:—-]?\s*\S+"
)
_SMS_TEXT = re.compile(
    r"^\s*(?:СМС|SMS|сообщение)\s+от\s+\S+.*?[:—]\s*\S+", re.IGNORECASE
)
_CHAT_VISUAL = re.compile(r"^\s*(?:>|[│┃║╭┌└╰])")


def messenger_layout_errors(skin: str, narration: str) -> list[str]:
    """Reject *plain* sender/time messages that lack visual message framing.

    Does not enforce one invented bubble style: Markdown blockquotes, Unicode
    chat boxes and explicit phone/message frames are valid alternatives.
    """
    if not re.search(r"мес+ен[дж]?|мессендж", skin, re.IGNORECASE):
        return []
    result = []
    main = narration.split(_CHOICE[0], 1)[0]
    lines = main.splitlines()
    for idx, line in enumerate(lines):
        value = line.strip()
        if not value:
            continue
        if not (_SENDER_TIME.match(value) or _SMS_TEXT.match(value)):
            continue
        # The frame may begin on the preceding line or be an explicit
        # message bubble/quote on the sender line itself.
        visual = any(_CHAT_VISUAL.match(lines[i]) for i in (
            idx, max(0, idx - 1), min(len(lines) - 1, idx + 1)
        ))
        if not visual:
            result.append(
                f"Сообщение в строке {idx + 1} оформлено обычным текстом: "
                "требуется визуально выделенный блок мессенджера"
            )
    return result


def _model_choice_count(skin: str, heading: str) -> int:
    start = skin.find(heading)
    if start < 0:
        return 0
    next_lines = skin[start:].splitlines()[1:]
    count = 0
    for line in next_lines:
        if line.strip() == "-":
            count += 1
        elif line.strip().startswith(("💙", "🔹", "🔸", "***")):
            break
    return count


def layout_errors(skin: str, narration: str, turn: int) -> list[str]:
    """Only objectively decidable constraints, inferred from CURRENT skin."""
    if not is_formatted_builder(skin):
        return []
    errors: list[str] = []
    text = narration.strip()
    header = text[:min(len(text), 1400)]
    fields = ("📖", "🗓️", "📌", "🌤️", "🎬", "👥", "👤", "👕", "🎒")
    last = -1
    for field in fields:
        pos = header.find(field, last + 1)
        if pos < 0:
            errors.append("Шапка: отсутствует или нарушен порядок " + field)
        else:
            last = pos
    points: list[int] = []
    for name in _CHOICE:
        pos = text.find(name)
        if pos < 0:
            errors.append("Отсутствует блок " + name)
        points.append(pos)
    if all(p >= 0 for p in points) and points != sorted(points):
        errors.append("Нарушен порядок трёх блоков выбора")
    for i, heading in enumerate(_CHOICE):
        if points[i] < 0:
            continue
        stop = min((x for x in points[i + 1:] if x > points[i]), default=-1)
        if stop < 0:
            next_mark = text.find("***", points[i])
            next_state = text.find("Состояние:", points[i])
            stop = min((x for x in (next_mark, next_state) if x > points[i]), default=len(text))
        choice_text = text[points[i] + len(heading):stop]
        count = sum(bool(re.match(r"^\s*[-*]\s+\S", line)) for line in choice_text.splitlines())
        required = _model_choice_count(skin, heading)
        if required and count != required:
            errors.append(f"{heading} ожидалось {required} вариантов, найдено {count}")
    errors.extend(messenger_layout_errors(skin, narration))
    if "Состояние:" in skin and "Состояние:" not in text:
        errors.append("Отсутствует строка Состояние")
    turn_marker = re.compile(rf"\bХод\s+{turn}\s*·\s*цикл\s+{((turn - 1) % 15) + 1}/15\b")
    if "Ход {turn_number}" in skin and not turn_marker.search(text):
        errors.append("Неверный номер хода или цикл /15")
    if "***" in skin and text.count("***") < 2:
        errors.append("Нет обязательных разделителей ***")
    match = _RANGE.search(skin)
    if match and points[0] >= 0:
        # Remove header and footer: narration begins after the last header field.
        heading_end = header.find("🎒")
        narrative_start = text.find("\n", heading_end) if heading_end >= 0 else -1
        if narrative_start < 0:
            errors.append("Не найдено начало художественного текста после шапки")
        else:
            body = text[narrative_start:points[0]]
            body = re.sub(r"^\s*\*\*\*\s*", "", body)
            body = re.sub(r"\s*\*\*\*\s*$", "", body)
            amount = len(re.sub(r"\s", "", body))
            lower, upper = map(int, match.groups())
            if not lower <= amount <= upper:
                errors.append(
                    f"Объём художественного текста: {amount} символов без пробелов "
                    f"(требуется {lower}–{upper})")
    return errors


def report_errors(skin: str, report: list[dict]) -> list[str]:
    """Require a review result on EVERY current section, with a real note."""
    required = review_sections(skin)
    if not isinstance(report, list):
        return ["Отчёт проверки должен быть списком всех разделов Skin Builder"]
    errors: list[str] = []
    by_section: dict[str, dict] = {}
    for item in report:
        if not isinstance(item, dict) or set(item) != {"section", "result", "note"}:
            errors.append("Запись проверки должна содержать section, result, note")
            continue
        name = item["section"]
        if not isinstance(name, str) or name in by_section:
            errors.append("Дублирующийся или некорректный раздел в отчёте")
            continue
        by_section[name] = item
        if item["result"] not in ("pass", "not_applicable", "fail"):
            errors.append("Недопустимый итог проверки для " + name)
        if not isinstance(item["note"], str) or len(item["note"].strip()) < 12:
            errors.append("Нет содержательного комментария проверки для " + name)
        if item["result"] == "fail":
            errors.append("Не исправлено нарушение раздела " + name + ": " + str(item["note"])[:180])
    for name in required:
        if name not in by_section:
            errors.append("Не проверен раздел Skin Builder: " + name)
    for name in by_section:
        if name not in required:
            errors.append("В отчёте посторонний раздел: " + name)
    return errors
