# -*- coding: utf-8 -*-
import numpy as np
import pytest

from whisper_core.meeting import voice_memory
from fronts.desktop import i18n


class DummyProfile:
    def __init__(self, tmp_path):
        self.dir = tmp_path
        self.name = "default"

    @property
    def voice_memory_path(self):
        return self.dir / "voices.json"


def test_voice_memory_store_basic(tmp_path):
    profile = DummyProfile(tmp_path)
    assert voice_memory.load_voices(profile) == {}
    assert voice_memory.list_voices(profile) == []

    # 192-dim unit vector
    vec1 = np.zeros(192, dtype=np.float32)
    vec1[0] = 1.0

    voice_memory.add_or_update_voice(profile, "Oksana", vec1.tolist())

    voices = voice_memory.load_voices(profile)
    assert "Oksana" in voices
    assert voices["Oksana"]["sample_count"] == 1
    assert len(voices["Oksana"]["centroid"]) == 192

    lst = voice_memory.list_voices(profile)
    assert len(lst) == 1
    assert lst[0]["name"] == "Oksana"
    assert lst[0]["samples_count"] == 1


def test_voice_memory_drift_and_normalization(tmp_path):
    profile = DummyProfile(tmp_path)

    v1 = np.zeros(192, dtype=np.float32)
    v1[0] = 1.0
    voice_memory.add_or_update_voice(profile, "Mykola", v1.tolist())

    # Second vector close to v1
    v2 = np.zeros(192, dtype=np.float32)
    v2[0] = 0.8
    v2[1] = 0.6
    voice_memory.add_or_update_voice(profile, "Mykola", v2.tolist())

    voices = voice_memory.load_voices(profile)
    assert "Mykola" in voices
    assert voices["Mykola"]["sample_count"] == 2
    # Verify L2 norm == 1.0
    norm = np.linalg.norm(voices["Mykola"]["centroid"])
    assert pytest.approx(norm, 1e-5) == 1.0


def test_voice_memory_matching_threshold(tmp_path):
    profile = DummyProfile(tmp_path)

    v_olena = np.zeros(192, dtype=np.float32)
    v_olena[0] = 1.0
    voice_memory.add_or_update_voice(profile, "Olena", v_olena.tolist())

    saved = voice_memory.load_voices(profile)

    # Test 1: Match with high cosine similarity (~0.98 >= 0.6)
    query_match = np.zeros(192, dtype=np.float32)
    query_match[0] = 0.98
    query_match[1] = 0.2
    query_match /= np.linalg.norm(query_match)

    name, sim = voice_memory.match_voice(query_match.tolist(), saved)
    assert name == "Olena"
    assert sim >= 0.6

    # Test 2: No match (orthogonal vector, sim ~0 < 0.6)
    query_no_match = np.zeros(192, dtype=np.float32)
    query_no_match[10] = 1.0

    name_none, sim_low = voice_memory.match_voice(query_no_match.tolist(), saved)
    assert name_none is None
    assert sim_low < 0.6


def test_voice_memory_deletion(tmp_path):
    profile = DummyProfile(tmp_path)
    v = [1.0] + [0.0] * 191

    voice_memory.add_or_update_voice(profile, "Serhiy", v)
    voice_memory.add_or_update_voice(profile, "Iryna", v)

    assert len(voice_memory.load_voices(profile)) == 2

    # Delete one
    res = voice_memory.delete_voice(profile, "Serhiy")
    assert res is True
    assert "Serhiy" not in voice_memory.load_voices(profile)
    assert "Iryna" in voice_memory.load_voices(profile)

    # Clear all
    count = voice_memory.clear_voices(profile)
    assert count == 1
    assert voice_memory.load_voices(profile) == {}


def test_pending_centroids_roundtrip_and_enroll_once(tmp_path):
    """Pending-сховище: запис за згоди, take забирає й видаляє запис, файл зникає
    коли порожній. Живе у теці профілю, ПОЗА текою сесії."""
    profile = DummyProfile(tmp_path)
    sid = "2026-07-23_10-00-00"
    voice_memory.save_pending_centroids(profile, sid, {
        "speaker_01": [1.0, 0.0],
        "speaker_02": [0.0, 1.0],
    })
    pending = tmp_path / "voice_pending" / (sid + ".json")
    assert pending.is_file()

    c1 = voice_memory.take_pending_centroid(profile, sid, "speaker_01")
    assert c1 == [1.0, 0.0]
    # enroll-once: другий take того самого мовця — вже нічого
    assert voice_memory.take_pending_centroid(profile, sid, "speaker_01") is None
    # інший мовець ще на місці
    assert voice_memory.take_pending_centroid(profile, sid, "speaker_02") == [0.0, 1.0]
    # файл прибрано, коли записів не лишилось
    assert not pending.exists()


def test_save_pending_centroids_empty_writes_nothing(tmp_path):
    profile = DummyProfile(tmp_path)
    voice_memory.save_pending_centroids(profile, "sess", {})
    assert not (tmp_path / "voice_pending").exists()


def test_clear_voices_also_clears_pending(tmp_path):
    profile = DummyProfile(tmp_path)
    voice_memory.add_or_update_voice(profile, "Ігор", [1.0] + [0.0] * 191)
    voice_memory.save_pending_centroids(profile, "sess", {"speaker_01": [1.0, 0.0]})
    assert (tmp_path / "voice_pending" / "sess.json").is_file()
    voice_memory.clear_voices(profile)
    assert voice_memory.load_voices(profile) == {}
    assert not (tmp_path / "voice_pending" / "sess.json").exists()


def test_voice_pending_not_in_profile_transfer_allowlist():
    """Гарантія Т42: біометричне pending-сховище не переноситься між профілями."""
    from whisper_core import settings_io
    assert "voice_pending" not in settings_io._PROFILE_INCLUDE
    assert "voices.json" not in settings_io._PROFILE_INCLUDE


def test_format_voice_date_handles_int_epoch():
    """updated_at зберігається як int epoch → форматувальник дати не має падати
    (регрес: старий v.get('updated_at','')[:10] кидав TypeError на int)."""
    import os
    import time
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from fronts.desktop.pages.settings import _format_voice_date

    ts = int(time.time())
    assert _format_voice_date(ts) == time.strftime("%Y-%m-%d", time.localtime(ts))
    # відсутнє/некоректне значення → порожньо, без винятку
    assert _format_voice_date(None) == ""
    assert _format_voice_date("") == ""
    assert _format_voice_date(0) == ""
    assert _format_voice_date(-5) == ""


def test_voice_memory_list_row_render_with_real_store(tmp_path):
    """Реальний voices.json (updated_at=int) → рядок таблиці «Збережені голоси»
    будується без TypeError і показує дату."""
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from fronts.desktop.pages.settings import _format_voice_date
    from fronts.desktop.i18n import tr

    profile = DummyProfile(tmp_path)
    v = [1.0] + [0.0] * 191
    voice_memory.add_or_update_voice(profile, "Наталя", v)
    row = voice_memory.list_voices(profile)[0]

    date_str = _format_voice_date(row.get("updated_at"))
    assert date_str  # непорожня дата
    cell = tr("voice_memory_updated", date=date_str) if date_str else ""
    assert date_str in cell


def test_voice_memory_table_headers_render_translated_text():
    """Жива SettingsPage показує людські заголовки, а не fallback snake_case."""
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    from PySide6.QtCore import QCoreApplication, QEvent, QTimer
    from PySide6.QtWidgets import QApplication

    from fronts.desktop.pages.settings import SettingsPage
    from tests.test_settings_performance import _InstallerController

    class VoiceMemoryController(_InstallerController):
        def list_voice_memories(self):
            return [{
                "name": "Наталя",
                "samples_count": 1,
                "updated_at": 1_700_000_000,
            }]

    app = QApplication.instance() or QApplication([])
    previous_language = i18n.current_language()
    page = None
    try:
        i18n.set_language("uk")
        page = SettingsPage(VoiceMemoryController())
        page._tabs.setCurrentIndex(4)  # вкладка «Нарада», де живе таблиця
        page.show()
        app.processEvents()

        headers = [
            page._vmem_table.horizontalHeaderItem(column).text()
            for column in range(page._vmem_table.columnCount())
        ]
        assert page._vmem_table.isVisible()
        assert headers == ["Назва", "# прикладів", "Дата", "Дії"]
    finally:
        i18n.set_language(previous_language)
        if page is not None:
            for timer in page.findChildren(QTimer):
                timer.stop()
            page.close()
            page.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        app.processEvents()


def test_i18n_keys():
    keys = [
        "voice_memory_enabled",
        "voice_memory_hint",
        "voice_memory_title",
        "voice_memory_empty",
        "voice_memory_delete",
        "voice_memory_clear_all",
        "voice_memory_clear_confirm",
        "voice_memory_samples",
        "voice_memory_updated",
    ]
    for k in keys:
        uk_val = i18n.STRINGS["uk"].get(k)
        en_val = i18n.STRINGS["en"].get(k)
        assert uk_val, f"Key {k} missing in UK i18n"
        assert en_val, f"Key {k} missing in EN i18n"
