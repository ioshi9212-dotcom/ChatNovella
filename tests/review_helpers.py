"""Complete scene fixtures for storage tests that used bare narration strings."""
import json
import re
from chatnovella.scene_review import REVIEW_CHECKS
from chatnovella.store import StateError


def scene(body="Рината оглядела зал.", relationships="нет участвующих NPC"):
    return (
        "📖 Проверочная новелла · весна ·\n"
        "🗓️ День 1 · четверг, 02.05.2024, 12:00\n"
        "📌 Читальный зал · 🌤️ Погода: солнечно\n"
        "🎬 Сцена: Рината осматривается и выбирает дальнейшее действие\n"
        "👤 Рината 👕 Одежда, волосы: джинсы, хвост 🎒 Инвентарь: телефон\n"
        "----------------------------------------\n"
        + body + "\n\n"
        "В дальнем углу хлопнула дверь. Рината заметила свежий след у порога.\n"
        "Рината остановилась у столика и прислушалась к коридору.\n"
        "За стеллажом скрипнула тяжёлая дверь без видимого толчка.\n"
        "На полу у порога лежала записка с сегодняшней датой.\n"
        "В тусклом свете было видно, как дрожит край записки.\n"
        "Тонкая полоска света медленно прошла по нижнему краю двери.\n\n"
        "Что я могу сделать:\n- Открыть окно\n- Подойти к столу\n- Проверить дверь\n"
        "Что я могу сказать:\n- Кто здесь?\n- Я подожду у двери.\n- Вы что-то потеряли?\n"
        "Что я могу подумать:\n- Здесь тихо.\n- Пора найти нужный стол.\n- Лучше осмотреть выход.\n"
        "Состояние: спокойна, внимательна\n"
        "Отношения: " + relationships + "\n"
    )


def checks():
    result = {key: {"status": "pass", "evidence": f"{key}: в проверочном тексте использованы данные тестового состояния."}
              for key in REVIEW_CHECKS}
    result["direction"]["evidence"] = (
        "Опора: ситуация Ринаты в читальном зале; "
        "Сдвиг: В дальнем углу хлопнула дверь; "
        "Зацепка: Рината заметила свежий след у порога"
    )
    return result


def relationship_report(report, labels, files):
    """Fill storage fixtures; relationship tests supply their own decisions."""
    ids = set(labels) | {name.split("/")[1] for name in files if name.endswith("/relations.md")}
    decisions = {}
    for npc_id in ids:
        text = files.get(f"characters/{npc_id}/relations.md", "")
        changed = bool(re.search(r"/[+-]?[1-9]\d*\s*$", text, re.M))
        decisions[npc_id] = {"status": "changed" if changed else "unchanged",
                             "reason": "Событие тестовой сцены изменило отношение." if changed else "В тестовой сцене нет нового основания для сдвига отношений."}
    report["npc"].setdefault("relationships", json.dumps(decisions, ensure_ascii=False))
    return report



def scorecard(reader_text="(Оглядеться)"):
    """Deliberately concrete synthetic proof for test-only miniature scenes."""
    quotes = {
        "pov_autonomy": "Рината остановилась у столика и прислушалась к коридору.",
        "npc_character_initiative": "За стеллажом скрипнула тяжёлая дверь без видимого толчка.",
        "plot_initiative": "В дальнем углу хлопнула дверь.",
        "pacing": "На полу у порога лежала записка с сегодняшней датой.",
        "genre_detail": "В тусклом свете было видно, как дрожит край записки.",
        "no_blurring_or_fade": "Рината заметила свежий след у порога.",
    }
    reasons = {
        "pov_choice": "Действия из ввода читателя сохранены; принципиальные решения за Ринату не сделаны.",
        "canon_and_knowledge": "В тестовой новелле отсутствуют тайные сведения или противоречащие её канону факты.",
        "relationship_consistency": "Показатели отношений и список участников подтверждены сохранёнными тестовыми карточками.",
        "unjustified_omission_or_fade": "Значимые события не обрываются на монтаже и не заменяются расплывчатыми эвфемизмами.",
    }
    return {
        "axes": {
            name: {"score": 2, "quote": quote,
                   "basis": "Тестовая сцена содержит проверяемое событие: " + quote,
                   "issue": "", "fix": ""}
            for name, quote in quotes.items()
        },
        "critical": {
            name: {"violation": False, "reason": reason}
            for name, reason in reasons.items()
        },
        "reader_trace": {
            "input_quote": reader_text,
            "scene_quote": "Рината остановилась у столика и прислушалась к коридору.",
            "explanation": "Ввод читателя отражён в действиях Ринаты при осмотре обстановки; сюжетная инициатива сохранена за миром.",
        },
        "fade_notes": [],
        "pov_actions": [],
    }

def reviewed_commit(store, session_id, expected_version, reader_text, narration,
                    files, timeline_events=None, timeline_no_change_reason="",
                    packet_id=None, relationship_labels=None,
                    relationships="нет участвующих NPC"):
    payload = dict(
        session_id=session_id, expected_version=expected_version,
        reader_text=reader_text,
        narration=scene(narration, relationships).replace(
            "Тонкая полоска света медленно прошла по нижнему краю двери.",
            f"Тонкая полоска света медленно прошла по нижнему краю двери. "
            f"На нижнем поле страницы стоял номер ревизии {expected_version}.",
        ),
        files=files,
        timeline_events=timeline_events,
        timeline_no_change_reason=timeline_no_change_reason, packet_id=packet_id,
    )
    scan = store.prepare_turn(session_id, [])
    for index in range(scan["count"]):
        store.read_chunk(scan["packet_id"], index)
    store.plan_scene(session_id, expected_version, reader_text, scan["packet_id"], {
        "basis": "Проверочная сцена в читальном зале без активных арок",
        "active_arcs": [], "candidate_npcs": [],
        "time_span": "Короткая сцена без продолжительного временного перехода",
        "npc_initiative": "Никто из NPC не участвует, движение создаёт мир",
        "intended_shift": "Рината обнаруживает новое обстоятельство внутри зала",
        "possible_hook": "Свежий след у порога даёт непосредственное продолжение",
    })
    report = relationship_report(checks(), relationship_labels or {}, files)
    result = store.review_scene(**payload, checks=report,
                                relationship_labels=relationship_labels or {},
                                scorecard=scorecard(reader_text))
    if not result["approved"]:
        raise StateError("; ".join(result["issues"]))
    return store.commit_turn(**payload, review_id=result["review_id"])
