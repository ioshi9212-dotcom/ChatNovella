"""Complete scene fixtures for storage tests that used bare narration strings."""
from chatnovella.scene_review import REVIEW_CHECKS
from chatnovella.semantic_review import SEMANTIC_KEYS
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




def fake_semantic_reviewer(context):
    """TEST ONLY: deterministic independent-review substitute, never used by app."""
    assert "draft_scene" in context and "state" in context and "rules" in context
    return {
        "summary": "Тестовый отчёт о соответствии черновика без реального модельного вызова.",
        "findings": {
            key: {
                "status": "na" if key in ("scene_depth",) else "pass",
                "evidence": ("Нет поцелуев, хоррора или экшена, требующих иной детализации."
                             if key == "scene_depth" else
                             f"В тестовом черновике проверен пункт {key} по реальным файлам"),
                "fix": "",
            } for key in SEMANTIC_KEYS
        },
    }

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
    store.semantic_reviewer = fake_semantic_reviewer
    result = store.review_scene(**payload, checks=checks(), relationship_labels=relationship_labels or {})
    if not result["approved"]:
        raise StateError("; ".join(result["issues"]))
    return store.commit_turn(**payload, review_id=result["review_id"])
