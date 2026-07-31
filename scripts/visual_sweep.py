"""Скрін-прохід «Балачок»: QWidget.grab() кожної сторінки й досяжного діалогу,
обидві мови (uk+en), матриця DPI 100/125/150%.

Навіщо: детектор (visual_gate.py) ловить обрізання за логічною геометрією, але
НЕ бачить дефектів дробового масштабу (округлення пікселів при 125/150% — див.
RESEARCH розд.4) і стилістичних дрібниць. Їх бачить людське око на реальних
знімках. Цей прохід дає ті знімки: людина (Микола) переглядає теку й приймає.

Платформа windows (offscreen бреше про шрифти) — фіксується імпортом visual_gate.
DPI: QT_SCALE_FACTOR читається Qt ОДИН раз на старті процесу й МНОЖИТЬСЯ на
системний масштаб, тож кожен масштаб — окремий процес (--all-scales спавнить трьох
дітей із QT_SCALE_FACTOR=1.0/1.25/1.5 і PassThrough-округленням).

Знімки — НЕ в репо і НЕ на Desktop: типово тимчасова тека, або --out.

Запуск (з кореня worktree):
  .venv\\Scripts\\python scripts\\visual_sweep.py --all-scales --out "<тека>"
  .venv\\Scripts\\python scripts\\visual_sweep.py            # лише поточний масштаб
"""
import os
import sys
import subprocess
import shutil
import tempfile
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

# Імпорт visual_gate ВИСТАВЛЯЄ QT_QPA_PLATFORM=windows ДО PySide6 — тому першим.
import visual_gate  # noqa: E402

DEFAULT_OUT = Path(tempfile.gettempdir()) / "balachky-visual-sweep"
SCALES = ("1.0", "1.25", "1.5")


def _save(pm, path: Path):
    if pm.isNull() or pm.width() == 0 or pm.height() == 0:
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    pm.save(str(path), "PNG")
    return pm.width(), pm.height(), path.stat().st_size


def _grab(widget, path: Path, app=None):
    """Subtree-grab одного віджета (діалоги) → PNG. QDialog має непрозоре тло
    SURFACE (QSS), тож grab піддерева самодостатній. Повертає (w,h,байти) або None."""
    try:
        if app is not None:
            visual_gate._process(app, 3)
        return _save(widget.grab(), path)
    except Exception as e:
        print(f"  ! grab {path.name}: {e}")
        return None


def _grab_window(win, sidebar, path: Path, app):
    """Знімок УСЬОГО вікна (сайдбар + сторінка) БЕЗ привида попередньої сторінки.

    Чому не win.grab(): offscreen-вікно (WA_DontShowOnScreen) при grab() блитить
    свій бекстор, де лишаються пікселі ПОПЕРЕДНЬОЇ сторінки — прозорі сторінки
    (Mica) не стирають старе. Доведено пробником: НІ repaint, НІ багато
    processEvents, НІ round-trip це не чистять (round-trip лише переносить привид
    на проміжну сторінку). А win.render() дає інший брак (розкладка нової сторінки
    ще не застосована + стара теж малюється).

    Робоче: subtree-grab кожного шматка ОКРЕМО (сайдбар і поточна сторінка
    рендеряться у ВЛАСНИЙ піксмап, повз бекстор → завжди чисто) і КОМПОЗ на тло
    теми (сайдбар=DEEP, контент=SURFACE — саме те, що Mica підкладає наживо).
    Сторінка прозора, тож її вміст лягає поверх SURFACE як у живому вікні."""
    from PySide6.QtGui import QPixmap, QPainter, QColor
    from fronts.desktop import theme
    try:
        visual_gate._process(app, 4)
        dpr = win.devicePixelRatioF()
        canvas = QPixmap(round(win.width() * dpr), round(win.height() * dpr))
        canvas.setDevicePixelRatio(dpr)
        canvas.fill(QColor(theme.SURFACE))
        p = QPainter(canvas)
        sb_pos = sidebar.mapTo(win, sidebar.rect().topLeft())
        p.fillRect(sb_pos.x(), sb_pos.y(), sidebar.width(), sidebar.height(),
                   QColor(theme.DEEP))
        p.drawPixmap(sb_pos, sidebar.grab())
        pg = win.pages.currentWidget()
        p.drawPixmap(pg.mapTo(win, pg.rect().topLeft()), pg.grab())
        p.end()
        return _save(canvas, path)
    except Exception as e:
        print(f"  ! grab_window {path.name}: {e}")
        return None


def _grab_splash(lang, path: Path, app):
    """Окремий кадр заставки (рецензія №2: splash теж має потрапляти у скрін-
    прохід). QSplashScreen напівпрозорий навколо картки — композит на темну
    підкладку, щоб медальйон-breakout і картка читались на знімку для огляду.
    У проході анімації вимкнено (visual_gate) → детермінований статичний кадр."""
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QPixmap, QPainter, QColor
    from fronts.desktop.splash import SplashScreen
    s = None
    try:
        s = SplashScreen(greet=False)
        s.setAttribute(Qt.WA_DontShowOnScreen, True)
        s.show()
        visual_gate._process(app, 3)
        grab = s.grab()
        canvas = QPixmap(grab.size())
        canvas.setDevicePixelRatio(grab.devicePixelRatio() or 1.0)
        canvas.fill(QColor(18, 18, 20))            # темна підкладка під прозорий верх
        p = QPainter(canvas)
        p.drawPixmap(0, 0, grab)
        p.end()
        return _save(canvas, path)
    except Exception as e:
        print(f"  ! splash {path.name}: {e}")
        return None
    finally:
        if s is not None:
            try:
                s._stop_motion(); s.close(); s.deleteLater()
            except Exception:
                pass
            visual_gate._process(app, 2)


def sweep_scale(out_dir: Path, langs, app, qt):
    """Один масштаб (поточний QT_SCALE_FACTOR): усі сторінки й діалоги, обидві мови."""
    from whisper_core import profiles
    from PySide6.QtCore import Qt

    scale = os.environ.get("QT_SCALE_FACTOR", "1.0")
    scale_dir = out_dir / f"scale-{scale}"
    if scale_dir.exists():
        shutil.rmtree(scale_dir, ignore_errors=True)
    scale_dir.mkdir(parents=True, exist_ok=True)

    sandbox = visual_gate._make_sandbox()
    _orig = profiles.list_profiles
    profiles.list_profiles = lambda root=None: _orig(sandbox)
    saved = []
    try:
        for lang in langs:
            win, _ctrl = visual_gate.open_main_window(lang, app, sandbox)
            sidebar = win.findChild(qt["QFrame"], "sidebar")
            # заставка — окремий кадр (назва локалізована, тож per-lang)
            res = _grab_splash(lang, scale_dir / f"{lang}_splash.png", app)
            if res:
                saved.append((f"{lang}_splash.png", res))
            # сторінки: знімаємо ВСЕ вікно (сайдбар + сторінка) на кожному пункті —
            # композитом (див. _grab_window), бо win.grab() offscreen лишає привид.
            for i in range(win.pages.count()):
                win.set_page(i)
                visual_gate._process(app, 4)
                pname = type(win.pages.widget(i)).__name__
                res = _grab_window(win, sidebar,
                                   scale_dir / f"{lang}_{i:02d}_{pname}.png", app)
                if res:
                    saved.append((f"{lang}_{i:02d}_{pname}.png", res))
            # діалоги: будуємо, показуємо поза екраном, знімаємо сам діалог
            for name, factory in visual_gate._dialog_factories(win, lang):
                try:
                    dlg = factory()
                except Exception as e:
                    print(f"  ! діалог {name}: {e}")
                    continue
                try:
                    dlg.setAttribute(Qt.WA_DontShowOnScreen, True)
                    dlg.show()
                    visual_gate._process(app, 3)
                    res = _grab(dlg, scale_dir / f"{lang}_dlg_{name}.png", app)
                    if res:
                        saved.append((f"{lang}_dlg_{name}.png", res))
                finally:
                    try:
                        dlg.close()
                        dlg.deleteLater()
                    except Exception:
                        pass
                    visual_gate._process(app, 2)
            visual_gate.close_main_window(win, app)
    finally:
        profiles.list_profiles = _orig
        shutil.rmtree(sandbox, ignore_errors=True)

    # звіт масштабу: скільки знято, найменший файл (діагностика «чорних» кадрів)
    print(f"\n[scale {scale}] знято {len(saved)} кадрів → {scale_dir}")
    tiny = [(n, r[2]) for n, r in saved if r[2] < 3000]
    if tiny:
        print(f"  ⚠ підозріло малі файли (<3КБ, можливо порожні): {tiny}")
    else:
        print("  усі кадри вагомі (≥3КБ) — не порожні")
    return len(saved), len(tiny)


def main():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    import argparse
    parser = argparse.ArgumentParser(description="Скрін-прохід Балачок (DPI-матриця)")
    parser.add_argument("--all-scales", action="store_true",
                        help="спавнити 3 процеси: QT_SCALE_FACTOR 1.0/1.25/1.5")
    parser.add_argument("--out", default=str(DEFAULT_OUT),
                        help="тека для знімків (НЕ репо, НЕ Desktop)")
    parser.add_argument("--langs", default="uk,en")
    args = parser.parse_args()

    if sys.platform != "win32":
        print("Скрін-прохід розрахований на Windows (Segoe UI/DWM).")
        return 0

    out_dir = Path(args.out)

    # --all-scales і ми НЕ дитина → спавнимо по процесу на масштаб (чистий старт,
    # бо Qt читає QT_SCALE_FACTOR лише раз). Дитина робить один масштаб.
    if args.all_scales and not os.environ.get("_SWEEP_CHILD"):
        out_dir.mkdir(parents=True, exist_ok=True)
        rc = 0
        for scale in SCALES:
            env = os.environ.copy()
            env["_SWEEP_CHILD"] = "1"
            env["QT_SCALE_FACTOR"] = scale
            env["QT_SCALE_FACTOR_ROUNDING_POLICY"] = "PassThrough"
            print(f"\n══════ масштаб {scale} (окремий процес) ══════")
            p = subprocess.run([sys.executable, str(Path(__file__).resolve()),
                                "--out", str(out_dir), "--langs", args.langs],
                               env=env)
            rc = rc or p.returncode
        print(f"\nГотово. Знімки: {out_dir}")
        sys.stdout.flush()
        return rc

    qt = visual_gate._lazy_qt()
    app = visual_gate._init_app(qt)
    langs = [x.strip() for x in args.langs.split(",") if x.strip()]
    sweep_scale(out_dir, langs, app, qt)
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)


if __name__ == "__main__":
    main()
