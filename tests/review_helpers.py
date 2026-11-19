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
        "В дальнем углу хлопнула дверь. Рината заметила свежий след у порога.\n\n"
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


def reviewed_commit(store, session_id, expected_version, reader_text, narration,
                    files, timeline_events=None, timeline_no_change_reason="",
                    packet_id=None, relationship_labels=None,
                    relationships="нет участвующих NPC"):
    payload = dict(
        session_id=session_id, expected_version=expected_version,
        reader_text=reader_text, narration=scene(narration, relationships), files=files,
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
    result = store.review_scene(**payload, checks=report, relationship_labels=relationship_labels or {})
    if not result["approved"]:
        raise StateError("; ".join(result["issues"]))
    return store.commit_turn(**payload, review_id=result["review_id"])
