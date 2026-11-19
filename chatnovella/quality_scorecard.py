"""Server-enforced evidence-based quality gate for unpublished ChatNovella scenes.

No external literary judge: the server validates verifiable evidence, threshold,
fatal admissions and suspicious fade transitions. Narrative quality still needs
the model's honest assessment; do not represent this as semantic proof.
"""
from __future__ import annotations

import re
from typing import Any

QUALITY_AXES = (
    "pov_autonomy",
    "npc_character_initiative",
    "plot_initiative",
    "pacing",
    "genre_detail",
    "no_blurring_or_fade",
)
CRITICAL_CHECKS = (
    "pov_choice",
    "canon_and_knowledge",
    "relationship_consistency",
    "unjustified_omission_or_fade",
)
MIN_SCORE = 11
MAX_SCORE = 12

# Only conspicuous transitions. A legitimate skip is allowed with an explanation.
# Short changes of focus, normal narrative ellipses and ordinary imagery are not
# automatically treated as evasive fade-outs.
_FADE = re.compile(
    r"\b(?:спустя\s+(?:несколько|пару|\d+)\s+(?:часов|дней|минут)|"
    r"на\s+следующее\s+утро|наутро|на\s+следующий\s+день|"
    r"дальше\s+всё\s+произошло\s+само|"
    r"ночь\s+скрыла\s+(?:всё|все|остальное)|"
    r"темнота\s+скрыла\s+(?:всё|все|остальное)|"
    r"остальное\s+осталось\s+за\s+кадром|"
    r"между\s+ними\s+(?:всё|все|что-то)\s+(?:изменилось|стало\s+иначе))\b", re.I
)


def _normalize(value: str) -> str:
    return " ".join(re.sub(r"\*\*", "", value).casefold().split())


def narrative_body(narration: str) -> str:
    """Only artistic prose, excluding header and post-scene choice lists."""
    lines = narration.splitlines()
    index = next((i for i, line in enumerate(lines)
                  if line.strip().replace("**", "") == "Что я могу сделать:"), -1)
    if index < 0:
        return ""
    return "\n".join(lines[5:index])


def check_scorecard(
    card: Any, narration: str, reader_text: str,
) -> tuple[list[str], dict[str, Any]]:
    """Inspect structured proof, returning issues and server-computed score.

    A claim of 2/2 is never accepted without an excerpt of the ACTUAL draft.
    We do NOT claim to identify subtle psychological or literary mistakes.
    """
    issues: list[str] = []
    report: dict[str, Any] = {"total": None, "minimum": MIN_SCORE,
                              "maximum": MAX_SCORE, "critical": False,
                              "fade_cues": []}
    if not isinstance(card, dict) or set(card) != {
        "axes", "critical", "reader_trace", "fade_notes"
    }:
        return ["scorecard: нужны axes, critical, reader_trace, fade_notes"], report
    body = _normalize(narrative_body(narration))
    reader = _normalize(reader_text)
    axes = card["axes"]
    if not isinstance(axes, dict) or set(axes) != set(QUALITY_AXES):
        return ["scorecard.axes: нужны все шесть направлений"], report
    total = 0
    quotes: list[str] = []
    quote_spans: list[tuple[int, int]] = []
    for key in QUALITY_AXES:
        item = axes[key]
        if not isinstance(item, dict) or set(item) != {
            "score", "quote", "basis", "issue", "fix"
        }:
            issues.append(f"scorecard.{key}: нужны score/quote/basis/issue/fix")
            continue
        score = item["score"]
        if type(score) is not int or score not in (0, 1, 2):
            issues.append(f"scorecard.{key}: балл должен быть 0, 1 или 2")
            continue
        total += score
        quote, basis = item["quote"], item["basis"]
        defect, fix = item["issue"], item["fix"]
        if (not isinstance(quote, str) or not 18 <= len(quote.strip()) <= 280
                or _normalize(quote) not in body):
            issues.append(f"scorecard.{key}: нужна дословная цитата 18–280 символов из художественной части, не из вариантов")
        else:
            normalized_quote = _normalize(quote)
            quotes.append(normalized_quote)
            at = body.find(normalized_quote)
            quote_spans.append((at, at + len(normalized_quote)))
            if key == "plot_initiative" and normalized_quote in reader:
                issues.append(
                    "scorecard.plot_initiative: событие полностью процитировано из ввода читателя; "
                    "нужен самостоятельный поступок NPC или сдвиг мира"
                )
        if not isinstance(basis, str) or not 35 <= len(basis.strip()) <= 650:
            issues.append(f"scorecard.{key}: объясни на конкретном событии/каноне (35–650 символов)")
        if not isinstance(defect, str) or not isinstance(fix, str):
            issues.append(f"scorecard.{key}: issue и fix должны быть строками")
        elif score < 2 and (not 25 <= len(defect.strip()) <= 450
                            or not 25 <= len(fix.strip()) <= 450):
            issues.append(f"scorecard.{key}: при 0/1 назови конкретный дефект и исправление (25–450 символов)")
        elif score == 2 and (defect.strip() or fix.strip()):
            issues.append(f"scorecard.{key}: обнаруженный дефект несовместим с оценкой 2/2")
    if len(set(quotes)) < 4:
        issues.append("scorecard.evidence: нужно не менее четырёх разных цитат, а не одно подтверждение на всё")
    distinct_spans = 0
    last_end = -1
    # One sentence chopped into four overlapping quotes is not four proofs.
    for start, end in sorted(quote_spans, key=lambda span: span[1]):
        if start >= last_end:
            distinct_spans += 1
            last_end = end
    if distinct_spans < 4:
        issues.append("scorecard.evidence: нужно не менее четырёх непересекающихся фрагментов сцены; нарезка одной фразы на цитаты не считается")
    report["total"] = total

    critical = card["critical"]
    if not isinstance(critical, dict) or set(critical) != set(CRITICAL_CHECKS):
        issues.append("scorecard.critical: обязательно отдельно оценить четыре критические ошибки")
        report["critical"] = True
    else:
        for key in CRITICAL_CHECKS:
            value = critical[key]
            if (not isinstance(value, dict) or set(value) != {"violation", "reason"}
                    or type(value["violation"]) is not bool
                    or not isinstance(value["reason"], str)
                    or not 28 <= len(value["reason"].strip()) <= 550):
                issues.append(f"scorecard.critical.{key}: нужны violation=true/false и конкретное основание")
                report["critical"] = True
                continue
            if value["violation"]:
                issues.append(f"scorecard.critical.{key}: {value['reason']}; исправь до сохранения")
                report["critical"] = True

    trace = card["reader_trace"]
    if not isinstance(trace, dict) or set(trace) != {
        "input_quote", "scene_quote", "explanation"
    }:
        issues.append("scorecard.reader_trace: сопоставь буквальный ввод читателя с его воплощением в сцене")
    else:
        inp, observed, why = (
            trace["input_quote"], trace["scene_quote"], trace["explanation"]
        )
        if (not isinstance(inp, str) or not 1 <= len(inp.strip()) <= 240
                or _normalize(inp) not in reader):
            issues.append("scorecard.reader_trace: цитата ввода должна действительно быть во вводе читателя")
        if (not isinstance(observed, str) or len(observed.strip()) < 18
                or _normalize(observed) not in body):
            issues.append("scorecard.reader_trace: процитируй исполнение ввода именно из художественной части")
        if not isinstance(why, str) or not 30 <= len(why.strip()) <= 500:
            issues.append("scorecard.reader_trace: объясни связь ввода с фактически написанным (30–500 символов)")

    fade_notes = card["fade_notes"]
    cues = [match.group(0) for match in _FADE.finditer(body)]
    report["fade_cues"] = cues
    if not isinstance(fade_notes, list) or len(fade_notes) > 12:
        issues.append("scorecard.fade_notes: список объяснений сюжетных переходов (не более 12)")
    else:
        used: set[str] = set()
        for i, item in enumerate(fade_notes):
            if not isinstance(item, dict) or set(item) != {"quote", "reason"}:
                issues.append(f"scorecard.fade_notes[{i}]: нужны quote и reason")
                continue
            quote, reason = item["quote"], item["reason"]
            if (not isinstance(quote, str) or not isinstance(reason, str)
                    or len(reason.strip()) < 35 or len(reason.strip()) > 500
                    or _normalize(quote) not in body):
                issues.append(f"scorecard.fade_notes[{i}]: приведи точный фрагмент перехода и объясни, почему важное не затемнено")
            else:
                used.add(_normalize(quote))
        for cue in cues:
            if not any(_normalize(cue) in quote for quote in used):
                issues.append(f"scorecard.fade: найден переход «{cue}»; обоснуй осмысленный монтаж либо признай затемнение и исправь")

    if total < MIN_SCORE:
        issues.append(f"scorecard.total: {total}/{MAX_SCORE}; нужно минимум {MIN_SCORE}/{MAX_SCORE}")
    return issues, report
