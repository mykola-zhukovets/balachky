"""Точка входу для PyInstaller-збірки (Balachky.exe).

Еквівалент `python -m fronts.desktop`; окремий файл, бо PyInstaller
потребує скрипт, а не пакет.
"""
from fronts.desktop.app import main

if __name__ == "__main__":
    main()
