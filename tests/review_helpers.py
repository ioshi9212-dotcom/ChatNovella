"""Complete scene fixtures for storage tests that used bare narration strings."""
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
        "Сдвиг: Рината осмотрела зал и обнаружила доступные пути; "
        "Зацепка: у Ринаты осталось конкретное действие для продолжения сцены"
    )
    return result


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
    result = store.review_scene(**payload, checks=checks(), relationship_labels=relationship_labels or {})
    if not result["approved"]:
        raise StateError("; ".join(result["issues"]))
    return store.commit_turn(**payload, review_id=result["review_id"])
