"""Навігація та відтворення за мовцем у розшифровці наради — ЯДРО, без Qt.

Спека власника (``2026-07-31-СПЕКА-навігація-за-мовцями.md``, лише читати):
у довгій нараді (2-3 години, 1000-3000 реплік) людині часто треба почути лише
репліки ОДНОГО учасника, пропустивши паузи й чужі виступи. Тут — чиста
математика для цього сценарію, без жодного Qt-об'єкта, тож покривається
юнітами без живого плеєра чи вікна:

1. ``next_speaker_utterance_ms`` / ``prev_speaker_utterance_ms`` — сусідня
   репліка ОБРАНОГО мовця від поточної позиції плеєра (кнопки
   [▲ Попередня] / [▼ Наступна] панелі розшифровки).
2. ``playback_segments`` — репліки обраного мовця, злиті в монолітні
   відрізки відтворення: сусідні репліки з паузою не довшою за поріг
   зливаються в один відрізок, довша пауза лишає розрив (плеєр перескочить
   через нього в режимі «Грати лише обраного»).

``speaker`` скрізь — код мовця (``SPK_ME``/``SPK_OTHERS``/``speaker_01``…).
``None`` означає «без фільтра» — той самий регістр, що чіп «Усі» в UI: усі
репліки проходять, незалежно від мовця.
"""
from __future__ import annotations

# Поріг склеювання паузи за замовчуванням (спека §3.3): природна пауза для
# вдиху чи витримки після запитання — 1,5-4,5 с; 5 с лишає запас і різко
# скорочує кількість seek-операцій, не розриваючи фрази мовця.
GAP_DEFAULT_S = 5.0

# запас у мс на нечіткість таймкодів (як next_utterance_ms/prev_utterance_ms
# у fronts/desktop/pages/meeting.py) — рівно на поточній позиції не має
# перестрибувати саму себе.
_EPS_MS = 50


def _matches(speaker: str, wanted) -> bool:
    return wanted is None or speaker == wanted


def next_speaker_utterance_ms(pos_ms: int, utterances, speaker=None) -> "int | None":
    """Старт першої репліки ОБРАНОГО мовця строго ПІСЛЯ поточної позиції
    (запас 50 мс). ``speaker=None`` — будь-яка репліка. Немає такої — None."""
    for u in utterances:
        if not _matches(u.speaker, speaker):
            continue
        start_ms = float(u.start) * 1000
        if start_ms > pos_ms + _EPS_MS:
            return int(start_ms)
    return None


def prev_speaker_utterance_ms(pos_ms: int, utterances, speaker=None) -> "int | None":
    """Старт першої репліки ОБРАНОГО мовця строго ПЕРЕД поточною позицією
    (запас 50 мс). ``speaker=None`` — будь-яка репліка. Немає такої — None."""
    for u in reversed(list(utterances)):
        if not _matches(u.speaker, speaker):
            continue
        start_ms = float(u.start) * 1000
        if start_ms < pos_ms - _EPS_MS:
            return int(start_ms)
    return None


def playback_segments(utterances, speaker=None, gap_s: float = GAP_DEFAULT_S) -> list:
    """Репліки ОБРАНОГО мовця (``speaker=None`` — усі) → відрізки відтворення
    ``[(start_ms, end_ms), ...]`` у хронологічному порядку (вхід уже
    хронологічний — та сама гарантія, що ``stitch()``/``UtteranceListModel``).

    Сусідні репліки зливаються в ОДИН відрізок, якщо пауза між кінцем
    попередньої й початком наступної НЕ перевищує ``gap_s`` секунд; довша
    пауза лишає розрив між двома відрізками. Мовець без жодної репліки чи
    порожній вхід → ``[]``."""
    gap_ms = max(0.0, float(gap_s)) * 1000
    segments: list = []
    for u in utterances:
        if not _matches(u.speaker, speaker):
            continue
        start_ms = int(round(float(u.start) * 1000))
        end_ms = int(round(float(u.end) * 1000))
        if end_ms < start_ms:                    # захист від битих таймкодів
            end_ms = start_ms
        if segments and start_ms - segments[-1][1] <= gap_ms:
            prev_start, prev_end = segments[-1]
            segments[-1] = (prev_start, max(prev_end, end_ms))
        else:
            segments.append((start_ms, end_ms))
    return segments
