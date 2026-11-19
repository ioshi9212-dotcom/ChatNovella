"""Mechanical scene checks; character/canon judgments remain the model's report."""
from __future__ import annotations

from datetime import datetime
import re
from typing import Any

REVIEW_CHECKS = (
    "canon", "pov", "npc", "continuity", "direction", "knowledge",
    "skin", "persistence",
)
MAX_CORRECTIONS = 2
OPTION_HEADINGS = (
    "Что я могу сделать:", "Что я могу сказать:", "Что я могу подумать:",
)


def plain(line: str) -> str:
    return re.sub(r"^#{1,6}\s+", "", line.strip()).replace("**", "").strip()


def inspect_scene(
    narration: str, checks: dict[str, Any], relationship_labels: dict[str, str],
) -> tuple[list[str], dict[str, dict[str, tuple[int, int]]]]:
    """Return concrete issues and footer metrics, without judging literary meaning."""
    issues: list[str] = []
    if not isinstance(checks, dict) or set(checks) != set(REVIEW_CHECKS):
        issues.append("review.checks: нужны все восемь пунктов: " + ", ".join(REVIEW_CHECKS))
    else:
        evidence = []
        for key in REVIEW_CHECKS:
            item = checks[key]
            if (not isinstance(item, dict) or set(item) != {"status", "evidence"}
                    or item["status"] not in ("pass", "fail")
                    or not isinstance(item["evidence"], str)
                    or not 12 <= len(item["evidence"].strip()) <= 350):
                issues.append(f"review.{key}: нужны status=pass/fail и конкретное evidence (12–350 символов)")
                continue
            note = item["evidence"].strip()
            evidence.append(note.casefold())
            if note.casefold().rstrip(".! ") in {"всё соблюдено", "все соблюдено", "всё проверено", "все проверено"}:
                issues.append(f"review.{key}: общее заверение не заменяет проверку текста")
            if key == "direction" and item["status"] == "pass":
                parts = re.fullmatch(
                    r"Опора:\s*(.+?)\s*;\s*Сдвиг:\s*(.+?)\s*;\s*Зацепка:\s*(.+)",
                    note, re.I | re.S,
                )
                if not parts or any(len(part.strip()) < 12 for part in parts.groups()):
                    issues.append(
                        "review.direction: при pass нужны конкретные 'Опора: ...; "
                        "Сдвиг: ...; Зацепка: ...' (каждое поле от 12 символов)"
                    )
            if item["status"] == "fail":
                issues.append(f"review.{key}: {note}")
        if len(evidence) != len(set(evidence)):
            issues.append("review.checks: одинаковые заверения вместо отдельных результатов проверки")

    lines = [plain(line) for line in narration.splitlines() if line.strip()]
    header = lines[:5]
    header_patterns = (
        r"📖\s*\S.+·\s*\S+.*",
        r"🗓️?\s*День\s+[1-9]\d*\s*·\s*\S.+",
        r"📌\s*\S.+·.*Погода:\s*\S.+",
        r"🎬\s*Сцена:\s*\S.+",
        r"👤\s*\S.+👕\s*Одежда,\s*волосы:\s*\S.+🎒\s*Инвентарь:\s*\S.+",
    )
    for index, pattern in enumerate(header_patterns):
        if index >= len(header) or re.fullmatch(pattern, header[index]) is None:
            issues.append(f"skin.header.{index + 1}: отсутствует или не заполнена строка шапки Skin Builder")
    if len(header) >= 2:
        date = re.search(r"(\d{2}\.\d{2}\.\d{4}),\s*(\d{2}:\d{2})\s*$", header[1])
        if date is None:
            issues.append("skin.date: нужны календарная дата ДД.ММ.ГГГГ и время ЧЧ:ММ")
        else:
            try:
                datetime.strptime(" ".join(date.groups()), "%d.%m.%Y %H:%M")
            except ValueError:
                issues.append("skin.date: невозможная дата или время в шапке")
    if any(re.search(r"\{[^}]+\}|\[[^]]+\]", line) for line in header):
        issues.append("skin.header: вместо данных остались незаполненные шаблоны")

    positions = []
    for label in (*OPTION_HEADINGS, "Состояние:", "Отношения:"):
        found = [i for i, line in enumerate(lines) if line.startswith(label)]
        if len(found) != 1:
            issues.append(f"skin.blocks: раздел '{label}' должен встречаться ровно один раз")
            return issues, {}
        positions.append(found[0])
    if positions != sorted(set(positions)) or positions[0] <= 5:
        issues.append("skin.blocks: нужны художественный текст, три блока вариантов, состояние, отношения — в этом порядке")
        return issues, {}
    body = lines[5:positions[0]]
    if not any(not re.fullmatch(r"[-—_*]{3,}", line) for line in body):
        issues.append("skin.body: художественная часть пуста")
    if any(re.match(r"^[—–]\s*\S", line) for line in body):
        issues.append("skin.dialogue: прямая речь без имени; используй **Имя** — реплика")
    # A direction report must cite text that really occurs BEFORE the 3x3 list.
    # This verifies evidence exists, NOT whether it is narratively significant.
    direction = checks.get("direction") if isinstance(checks, dict) else None
    if isinstance(direction, dict) and direction.get("status") == "pass":
        report = direction.get("evidence", "")
        matched = re.fullmatch(
            r"Опора:\s*(.+?)\s*;\s*Сдвиг:\s*(.+?)\s*;\s*Зацепка:\s*(.+)",
            report.strip(), re.I | re.S,
        ) if isinstance(report, str) else None
        if matched:
            narrative = re.sub(r"\s+", " ", " ".join(body)).casefold()
            quotes = [
                re.sub(r"\s+", " ", matched.group(i).strip().strip('«»"')).casefold()
                for i in (2, 3)
            ]
            if any(part not in narrative for part in quotes):
                issues.append(
                    "review.direction: сдвиг и зацепка должны быть точными "
                    "фрагментами художественного текста, не вариантами 3x3"
                )
            if quotes[0] == quotes[1]:
                issues.append("review.direction: одна и та же фраза не доказывает и сдвиг, и зацепку")
    for index, label in enumerate(OPTION_HEADINGS):
        start, end = positions[index], positions[index + 1]
        if lines[start] != label:
            issues.append(f"skin.options: после '{label}' не должно быть служебного шаблона")
        options = []
        for line in lines[start + 1:end]:
            if re.fullmatch(r"[-—_*]{3,}", line):
                continue
            match = re.match(r"^(?:[-*•]\s+|\d+[.)]\s+)(\S.*)$", line)
            if match:
                options.append(match.group(1))
            elif options:
                options[-1] += " " + line
            else:
                issues.append(f"skin.options: '{label}' содержит текст вне списка")
        if len(options) != 3:
            issues.append(f"skin.options: '{label}' — требуется ровно 3 непустых варианта, получено {len(options)}")
        if len({option.casefold() for option in options}) != len(options):
            issues.append(f"skin.options: одинаковые варианты в '{label}'")
        if any(option.startswith(('"', '«', '(')) for option in options):
            issues.append(f"skin.options: варианты в '{label}' пишутся без внешних кавычек и скобок")

    state = " ".join([lines[positions[3]][len("Состояние:"):],
                      *lines[positions[3] + 1:positions[4]]]).strip()
    if not state or len(re.findall(r"[\w]+(?:[-’][\w]+)*", state)) > 10:
        issues.append("skin.state: состояние должно содержать от 1 до 10 слов")
    footer = [lines[positions[4]][len("Отношения:"):].strip(),
              *lines[positions[4] + 1:]]
    footer = [line for line in footer if line]
    if not isinstance(relationship_labels, dict) or any(
        not isinstance(key, str) or not isinstance(label, str)
        or not 1 <= len(label.strip()) <= 120 or "\n" in label
        for key, label in relationship_labels.items()
    ):
        issues.append("skin.relationships: relationship_labels должен сопоставлять ID NPC и имя/описание в нижнем блоке")
        return issues, {}
    labels = [plain(label).casefold() for label in relationship_labels.values()]
    if len(labels) != len(set(labels)):
        issues.append("skin.relationships: подписи разных NPC должны различаться")
    if not relationship_labels:
        if " ".join(footer).casefold() not in {
            "нет", "—", "нет участвующих npc", "нет участвующих нпс",
        }:
            issues.append("skin.relationships: при отсутствии участвующих NPC напиши 'нет участвующих NPC'")
        return issues, {}
    metrics = {}
    used = set()
    for npc_id, label in relationship_labels.items():
        pattern = re.compile(r"^(?:[-•]\s+)?" + re.escape(plain(label)) + r"\s*[-—:]\s*(.+)$", re.I)
        matches = [(i, pattern.fullmatch(line)) for i, line in enumerate(footer)
                   if pattern.fullmatch(line)]
        if len(matches) != 1:
            issues.append(f"skin.relationships: нужна одна отдельная строка '{label} — показатель число/дельта'")
            continue
        index, match = matches[0]
        used.add(index)
        values = {}
        for item in match.group(1).split(";"):
            stat = re.fullmatch(r"\s*(.+?)\s+(\d+)\s*/\s*([+-]?\d+)\s*", item)
            if stat is None:
                issues.append(f"skin.relationships: {label}: вместо '{item.strip()}' нужны название, число и дельта")
                continue
            name = " ".join(stat[1].rstrip(":").casefold().split())
            if name in values:
                issues.append(f"skin.relationships: {label}: повторяется показатель '{name}'")
            values[name] = (int(stat[2]), int(stat[3]))
        metrics[npc_id] = values
    if used != set(range(len(footer))):
        issues.append("skin.relationships: лишняя или нераспознанная строка в нижнем блоке")
    return issues, metrics
