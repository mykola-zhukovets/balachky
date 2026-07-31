"""Агрегація станів та підрахунок розмірів усіх моделей за принципом «всі моделі в UI»."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import whisper_core.paths as paths
import whisper_core.models as stt_models
import whisper_core.meeting.diarization_models as diar_models
import whisper_core.protocol.model_manager as protocol_mm
import whisper_core.tts.voices as tts_voices
import whisper_core.punctuator as punc

if TYPE_CHECKING:
    from whisper_core.config import Config


@dataclass
class ModelHubItem:
    component_id: str           # "stt", "diarization", "protocol", "tts", "punctuator"
    title_key: str              # i18n key for component title
    is_downloaded: bool         # whether model is downloaded on disk
    size_bytes: int             # total disk size for this component
    active_name_key: str        # i18n key for active model/preset
    active_name_param: str      # optional parameter for active_name_key
    memory_note_key: str        # i18n key for memory footprint note
    recommended_preset: str     # preset ID / name for "Рекомендовано"
    is_recommended_active: bool  # whether recommended is currently active


def get_dir_size(p: str | Path | None) -> int:
    """Підрахунок розміру файлу чи папки в байтах."""
    if not p:
        return 0
    p = Path(p)
    if not p.exists():
        return 0
    if p.is_file():
        return p.stat().st_size
    total = 0
    try:
        for item in p.rglob('*'):
            if item.is_file() and not item.is_symlink():
                total += item.stat().st_size
    except Exception:
        pass
    return total


def get_models_hub_status(cfg: Config) -> list[ModelHubItem]:
    """Збір агрегованої інформації по 5 компонентах моделей."""
    items: list[ModelHubItem] = []

    # 1. STT (Speech-to-Text)
    stt_name = cfg.model_name or "large-v3-turbo"
    stt_repo = stt_models.repo_for(stt_name)
    stt_sz = stt_models.model_snapshot_size(cfg.model_dir, stt_repo) if stt_repo else 0
    stt_present = stt_sz > 0
    stt_key = "models_hub_preset_raw"
    stt_param = stt_name
    if stt_name == "large-v3-turbo":
        stt_key = "models_hub_preset_turbo"
        stt_param = ""
    elif stt_name == "large-v3":
        stt_key = "models_hub_preset_large_v3"
        stt_param = ""

    items.append(ModelHubItem(
        component_id="stt",
        title_key="models_hub_stt_title",
        is_downloaded=stt_present,
        size_bytes=stt_sz,
        active_name_key=stt_key,
        active_name_param=stt_param,
        memory_note_key="models_hub_stt_vram",
        recommended_preset="large-v3-turbo",
        is_recommended_active=(stt_name == "large-v3-turbo"),
    ))

    # 2. Diarization (Співрозмовники)
    diar_dir = paths.diarization_models_dir()
    diar_avail = diar_models.models_available(cfg.diarization_model_dir)
    diar_sz = get_dir_size(diar_dir)
    items.append(ModelHubItem(
        component_id="diarization",
        title_key="models_hub_diar_title",
        is_downloaded=diar_avail,
        size_bytes=diar_sz if diar_avail else 0,
        active_name_key="models_hub_preset_pyannote" if diar_avail else "models_hub_not_configured",
        active_name_param="",
        memory_note_key="models_hub_diar_vram",
        recommended_preset="pyannote",
        is_recommended_active=cfg.diarization_enabled and diar_avail,
    ))

    # 3. Protocol LLM (Протокол наради)
    proto_preset = cfg.protocol_model or "fast"
    # УВАГА: model_available(model_dir, preset_id) очікує ВЖЕ ГОТОВУ теку САМЕ
    # цього пресета (root/preset_id), не спільний корінь усіх пресетів —
    # інакше READY-маркер конкретної моделі ніколи не знаходився б (бачив би
    # лише спільний корінь, де його нема), і ModelsHub мовчки показував би
    # завантажену модель як «не завантажена» (знайдено аудитом 31.07.2026,
    # разом зі спекою фонового завантаження).
    proto_model_dir = paths.protocol_model_dir(proto_preset)
    proto_avail = protocol_mm.model_available(proto_model_dir, proto_preset)
    proto_sz = get_dir_size(proto_model_dir)
    if proto_preset == "fast":
        proto_key = "models_hub_preset_gemma_fast"
        proto_param = ""
    elif proto_preset == "quality":
        proto_key = "models_hub_preset_gemma_quality"
        proto_param = ""
    else:
        proto_key = "models_hub_preset_raw"
        proto_param = proto_preset

    items.append(ModelHubItem(
        component_id="protocol",
        title_key="models_hub_protocol_title",
        is_downloaded=proto_avail,
        size_bytes=proto_sz if proto_avail else 0,
        active_name_key=proto_key,
        active_name_param=proto_param,
        memory_note_key="models_hub_protocol_fast_ram",
        recommended_preset="fast",
        is_recommended_active=(proto_preset == "fast"),
    ))

    # 4. Voiceover TTS (Озвучення)
    tts_voice = cfg.tts_voice_uk or "styletts2_ua"
    tts_engine_installed = paths.tts_engine_exe_path().exists()
    tts_avail = tts_voices.voice_available(tts_voice) and (tts_engine_installed or not getattr(paths, "FROZEN", False))
    tts_sz = get_dir_size(paths.tts_voices_dir()) + get_dir_size(paths.tts_engine_dir())
    if tts_voice == "styletts2_ua":
        tts_key = "models_hub_preset_styletts2"
        tts_param = ""
    else:
        tts_key = "models_hub_preset_raw"
        tts_param = tts_voice

    items.append(ModelHubItem(
        component_id="tts",
        title_key="models_hub_tts_title",
        is_downloaded=tts_avail,
        size_bytes=tts_sz,
        active_name_key=tts_key,
        active_name_param=tts_param,
        memory_note_key="models_hub_tts_ram",
        recommended_preset="styletts2_ua",
        is_recommended_active=(tts_voice == "styletts2_ua" and cfg.tts_enabled),
    ))

    # 5. Punctuator (Пунктуація)
    punc_dir = paths.punctuator_model_dir()
    punc_avail = punc.model_available(punc_dir)
    punc_sz = get_dir_size(punc_dir)
    items.append(ModelHubItem(
        component_id="punctuator",
        title_key="models_hub_punc_title",
        is_downloaded=punc_avail,
        size_bytes=punc_sz,
        active_name_key="models_hub_preset_pcs" if punc_avail else "models_hub_not_downloaded",
        active_name_param="",
        memory_note_key="models_hub_punc_ram",
        recommended_preset="pcs_47lang",
        is_recommended_active=(cfg.punctuator_enabled and punc_avail),
    ))

    return items


def get_total_models_disk_size(cfg: Config) -> int:
    """Сумарний обсяг у байтах, зайнятий усіма папками моделей."""
    stt_cache = stt_models.resolve_cache_dir(cfg.model_dir)
    diar_dir = paths.diarization_models_dir()
    proto_dir = paths.protocol_models_dir()
    tts_dir = paths.tts_voices_dir()
    engine_dir = paths.tts_engine_dir()
    punc_dir = paths.punctuator_model_dir()

    return (get_dir_size(stt_cache) +
            get_dir_size(diar_dir) +
            get_dir_size(proto_dir) +
            get_dir_size(tts_dir) +
            get_dir_size(engine_dir) +
            get_dir_size(punc_dir))


def get_model_dirs(cfg: Config) -> list[tuple[str, Path]]:
    """Повертає список іменованих папок моделей (i18n_key, path)."""
    stt_cache = stt_models.resolve_cache_dir(cfg.model_dir) or paths.user_dir()
    return [
        ("models_hub_folder_stt", Path(stt_cache)),
        ("models_hub_folder_diar", paths.diarization_models_dir()),
        ("models_hub_folder_proto", paths.protocol_models_dir()),
        ("models_hub_folder_tts", paths.tts_voices_dir()),
        ("models_hub_folder_punc", paths.punctuator_model_dir()),
        ("models_hub_folder_user_dir", paths.user_dir()),
    ]

