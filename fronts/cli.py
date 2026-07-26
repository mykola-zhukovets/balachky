"""Headless CLI зі структурованим виводом — фундамент під майбутній MCP-сервер.

Підкоманди (argparse, лише stdlib):

    balachky transcribe <файл> [--model M] [--lang L] [--json]
    balachky dictionary list|add|remove [--profile P] [--json]
    balachky history search <запит> [--json]
    balachky export <сесія|файл> --format srt|txt|md [--out P]

Без --json — людський вивід (українською). З --json — JSON-об'єкт у stdout
(для агентів/скриптів); помилки завжди йдуть у stderr із ненульовим кодом.

Зворотна сумісність: стара форма «balachky [--profile P] <файл>…» без підкоманди
й далі працює — з підказкою про нову форму в stderr.

Ядро кожної підкоманди відокремлене від рушія: транскрипція інжектиться
параметром ``transcribe_fn`` (тести передають мок; справжній Engine
імпортується ліниво, лише коли реально потрібен).
"""
import argparse
import json
import sys
from pathlib import Path

from whisper_core import paths, profiles
from whisper_core.config import Config
from whisper_core.terms import (
    read_terms_dict, add_term, delete_term, load_terms,
)
from whisper_core.history import log_history
from whisper_core.search_index import SearchIndex
from whisper_core import export as export_mod

# База профілів — через whisper_core.paths (dev: корінь репо; frozen: USER_DIR)
ROOT = paths.profiles_root()

# Відомі підкоманди — для розрізнення нової форми від старої (позиційний файл).
_SUBCOMMANDS = ("transcribe", "dictionary", "history", "export")


# ───────────────────────────── рушій (ліниво) ─────────────────────────────
def _engine_transcribe(cfg, terms, path):
    """Справжня транскрипція одного файлу через Engine. Повертає п'ятірку
    (raw, final, duration, words, segments). Engine важкий (вантажить модель),
    тож імпорт — усередині, аби тести/інші підкоманди його не смикали."""
    from whisper_core.engine import Engine
    engine = Engine(cfg)
    return engine.transcribe(str(path), terms)


# ───────────────────────────── допоміжне ─────────────────────────────
def _err(msg):
    """Помилка → stderr (stdout лишається чистим для JSON/пайпів)."""
    print(msg, file=sys.stderr)


def _resolve_profile(name, root=ROOT):
    """Профіль за назвою або активний. → (Profile, None) чи (None, повідомлення)."""
    if name:
        prof = profiles.get(root, name)
        if prof is None:
            return None, f"Нема профілю “{name}”. Список: balachky dictionary list"
        return prof, None
    return profiles.get_active(root), None


def _segments_json(segs):
    """[(start, end, text), …] → [{"start","end","text"}, …] для JSON-виводу."""
    out = []
    for s in segs or ():
        start, end, text = s[0], s[1], s[2]
        out.append({"start": float(start), "end": float(end),
                    "text": (text or "").strip()})
    return out


def _print_json(obj):
    print(json.dumps(obj, ensure_ascii=False))


# ───────────────────────────── transcribe ─────────────────────────────
def cmd_transcribe(args, *, root=ROOT, transcribe_fn=_engine_transcribe):
    prof, msg = _resolve_profile(getattr(args, "profile", None), root)
    if prof is None:
        _err(msg)
        return 1
    p = Path(args.file)
    if not p.exists():
        _err(f"Нема файлу: {args.file}")
        return 1
    cfg = Config.load()
    if args.model:
        cfg.model_name = args.model
    if args.lang:
        cfg.language = args.lang
    terms = load_terms(prof.terms_path)
    raw, final, dur, _words, segs = transcribe_fn(cfg, terms, p)
    log_history(prof.history_path, raw, final, source="cli",
                enabled=prof.memory_enabled)
    if args.json:
        _print_json({"text": final, "segments": _segments_json(segs),
                     "model": cfg.model_name, "duration": float(dur)})
    else:
        print(f"### {p.name}  (аудіо {dur:.0f}s, модель {cfg.model_name})")
        print(f"  {final}")
        if final != raw:
            print(f"  (сире: {raw})")
    return 0


# ───────────────────────────── dictionary ─────────────────────────────
def cmd_dictionary(args, *, root=ROOT):
    prof, msg = _resolve_profile(getattr(args, "profile", None), root)
    if prof is None:
        _err(msg)
        return 1
    terms_path = prof.terms_path

    if args.op == "list":
        data = read_terms_dict(terms_path)
        if args.json:
            _print_json({"profile": prof.name,
                         "terms": [{"canon": c, "variants": list(v)}
                                   for c, v in sorted(data.items())]})
        else:
            print(f"Словник профілю “{prof.name}” — {len(data)} термінів:")
            for c, v in sorted(data.items()):
                tail = f"  ← {', '.join(v)}" if v else ""
                print(f"  {c}{tail}")
        return 0

    if args.op == "add":
        if not args.canon:
            _err("Вжиток: balachky dictionary add <канон> [варіант]")
            return 1
        add_term(terms_path, args.canon, args.variant or "")
        if args.json:
            _print_json({"added": args.canon, "variant": args.variant or ""})
        else:
            v = f" ← {args.variant}" if args.variant else ""
            print(f"Додано: {args.canon}{v}")
        return 0

    if args.op == "remove":
        if not args.canon:
            _err("Вжиток: balachky dictionary remove <канон>")
            return 1
        ok = delete_term(terms_path, args.canon)
        if args.json:
            _print_json({"removed": args.canon, "ok": ok})
        else:
            print(f"Видалено: {args.canon}" if ok
                  else f"Нема такого терміна (або він лише в людському файлі): {args.canon}")
        return 0 if ok else 1

    _err("Вжиток: balachky dictionary list|add|remove")
    return 1


# ───────────────────────────── history ─────────────────────────────
def cmd_history(args, *, root=ROOT):
    if args.op != "search":
        _err("Вжиток: balachky history search <запит>")
        return 1
    query = " ".join(args.query).strip()
    if not query:
        _err("Порожній запит. Вжиток: balachky history search <запит>")
        return 1
    profs = profiles.list_profiles(root)
    index = SearchIndex.build(history_paths=profs)
    results = index.search(query)
    if args.json:
        _print_json({"query": query,
                     "results": [{"kind": r.kind, "date": r.date,
                                  "snippet": r.snippet, "profile": r.profile,
                                  "score": r.score} for r in results]})
    else:
        print(f"Знайдено {len(results)} за запитом “{query}”:")
        for r in results:
            where = f" [{r.profile}]" if r.profile else ""
            print(f"  ({r.kind}{where}) {r.snippet}")
    return 0


# ───────────────────────────── export ─────────────────────────────
def _session_segments(session_dir):
    """transcript.json сесії наради → [(start, end, text), …] або None, якщо
    сесії/файлу нема. Кожна репліка вже несе start/end/text."""
    try:
        from whisper_core.meeting.session import read_artifact
        data = json.loads(read_artifact(
            Path(session_dir), "transcript.json").decode("utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, list):
        return None
    segs = []
    for u in data:
        text = (u.get("text") or "").strip()
        if not text or u.get("start") is None or u.get("end") is None:
            continue
        segs.append((float(u["start"]), float(u["end"]), text))
    return segs


def _render_export(segs, fmt, name):
    """Сегменти + формат → текст експорту. srt — субтитри; md — Markdown із
    frontmatter; txt — по одній репліці в рядок."""
    if fmt == "srt":
        return export_mod.to_srt(segs)
    if fmt == "md":
        text = " ".join(s[2] for s in segs)
        return export_mod.to_markdown(text, {"source": name}, segments=segs)
    # txt
    return "\n".join(s[2] for s in segs) + ("\n" if segs else "")


def cmd_export(args, *, root=ROOT, meetings_root=None,
               transcribe_fn=_engine_transcribe, confine_root=None):
    if meetings_root is None:
        meetings_root = paths.meetings_dir()
    target = args.target
    fmt = args.format

    # 1) сесія наради за id?
    # Traversal-гейт: "id" будує filesystem-шлях. "../../інша-тека" вислизнув би
    # за teky нарад і прочитав чужий transcript.json — резолв мусить лишитись у
    # межах meetings_root, інакше це НЕ сесія (падаємо у гілку файлу нижче).
    session_dir = Path(meetings_root) / target
    segs = None
    name = target
    if paths.safe_under(meetings_root, session_dir) and session_dir.is_dir():
        segs = _session_segments(session_dir)
        if segs is None:
            _err(f"Сесія “{target}” без розшифровки (нема transcript.json)")
            return 1
    else:
        # 2) інакше — аудіофайл: транскрибуємо й експортуємо його сегменти
        p = Path(target)
        # confine_root задає MCP-обгортка (недовірений контекст): файл-джерело
        # має лежати в межах даних застосунку. CLI (людина під своїм акаунтом)
        # передає None — довільний шлях дозволено.
        if confine_root is not None and not paths.safe_under(confine_root, p):
            _err("Шлях поза межами даних застосунку")
            return 1
        if not p.exists():
            _err(f"Нема сесії чи файлу: {target}")
            return 1
        prof, msg = _resolve_profile(getattr(args, "profile", None), root)
        if prof is None:
            _err(msg)
            return 1
        cfg = Config.load()
        terms = load_terms(prof.terms_path)
        _raw, _final, _dur, _words, segs = transcribe_fn(cfg, terms, p)
        name = p.name

    text = _render_export(segs, fmt, name)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8", newline="")
        print(f"Збережено: {args.out}")
    else:
        sys.stdout.write(text)
    return 0


# ───────────────────────────── парсер ─────────────────────────────
def build_parser():
    parser = argparse.ArgumentParser(
        prog="balachky",
        description="Розшифровка аудіо та робота зі словником/історією.")
    sub = parser.add_subparsers(dest="command")

    pt = sub.add_parser("transcribe", help="розшифрувати аудіофайл")
    pt.add_argument("file", help="шлях до аудіофайлу")
    pt.add_argument("--model", help="перекрити модель (напр. large-v3)")
    pt.add_argument("--lang", help="перекрити мову розшифровки (напр. uk)")
    pt.add_argument("--profile", help="профіль словника/пам'яті")
    pt.add_argument("--json", action="store_true", help="JSON у stdout")

    pd = sub.add_parser("dictionary", help="словник профілю")
    pd.add_argument("op", choices=("list", "add", "remove"))
    pd.add_argument("canon", nargs="?", help="канонічний термін (для add/remove)")
    pd.add_argument("variant", nargs="?", help="почутий варіант (для add)")
    pd.add_argument("--profile", help="профіль словника")
    pd.add_argument("--json", action="store_true", help="JSON у stdout")

    ph = sub.add_parser("history", help="пошук по історії розшифровок")
    ph.add_argument("op", choices=("search",))
    ph.add_argument("query", nargs="+", help="слова або дата запиту")
    ph.add_argument("--json", action="store_true", help="JSON у stdout")

    pe = sub.add_parser("export", help="експорт розшифровки")
    pe.add_argument("target", help="id сесії наради або аудіофайл")
    pe.add_argument("--format", required=True, choices=("srt", "txt", "md"))
    pe.add_argument("--out", help="файл виводу (без нього — stdout)")
    pe.add_argument("--profile", help="профіль (для розшифровки файлу)")

    return parser


# ───────────────────────────── стара форма ─────────────────────────────
def _legacy_main(argv, *, root=ROOT, transcribe_fn=_engine_transcribe):
    """Сумісність зі старою формою «[--profile P] <файл>…»: розшифрувати кожен
    файл. Підказуємо нову форму, але робимо роботу, щоб нічого не зламати."""
    _err("Підказка: нова форма — balachky transcribe <файл> [--json]. "
         "Стару форму поки підтримуємо.")
    prof_name = None
    if "--profile" in argv:
        i = argv.index("--profile")
        prof_name = argv[i + 1] if i + 1 < len(argv) else None
        del argv[i:i + 2]
    if not argv:
        _err("Вжиток: balachky transcribe <файл> [--json]")
        return 1
    prof, msg = _resolve_profile(prof_name, root)
    if prof is None:
        _err(msg)
        return 1
    cfg = Config.load()
    terms = load_terms(prof.terms_path)
    rc = 0
    for path in argv:
        p = Path(path)
        if not p.exists():
            _err(f"Нема файлу: {path}")
            rc = 1
            continue
        raw, final, dur, _words, _segs = transcribe_fn(cfg, terms, p)
        log_history(prof.history_path, raw, final, source="cli",
                    enabled=prof.memory_enabled)
        print(f"### {p.name}  (аудіо {dur:.0f}s)")
        print(f"  {final}")
        if final != raw:
            print(f"  (сире: {raw})")
    return rc


def main(argv=None):
    # Windows-консоль з дефолтним cp1251/866 падає на укр. апострофі U+02BC і
    # лапках “ ” у виводі — перевлаштовуємо потоки на UTF-8 (канон; той самий
    # патерн, що у mcp_server.py і protocol/worker.py). Звірка №5 18.07.
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    argv = list(argv if argv is not None else sys.argv[1:])
    if not argv:
        build_parser().print_help()
        return 1
    # стара форма: перший токен — не підкоманда й не прапорець допомоги
    if argv[0] not in _SUBCOMMANDS and argv[0] not in ("-h", "--help"):
        return _legacy_main(argv)
    args = build_parser().parse_args(argv)
    if args.command == "transcribe":
        return cmd_transcribe(args)
    if args.command == "dictionary":
        return cmd_dictionary(args)
    if args.command == "history":
        return cmd_history(args)
    if args.command == "export":
        return cmd_export(args)
    build_parser().print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
