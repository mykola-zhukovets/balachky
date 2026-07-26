# MCP-сервер Балачок

Локальний [Model Context Protocol](https://modelcontextprotocol.io) сервер, що
експонує функціонал Балачок як інструменти для ШІ-агентів (Claude Desktop тощо).
**100% офлайн**: транспорт — stdio (stdin/stdout), жодного мережевого порту чи
слухача; усі шляхи — лише в межах даних застосунку (профілі, теки нарад).

## Інструменти

| Інструмент | Аргументи | Що робить |
|---|---|---|
| `transcribe_file` | `path`, `model?`, `lang?`, `profile?` | Розшифрувати аудіофайл; текст + сегменти з таймкодами |
| `search_history` | `query` | Пошук по історії розшифровок усіх профілів |
| `list_dictionary` | `profile?` | Перелік термінів словника профілю |
| `add_dictionary_term` | `canon`, `variant?`, `profile?` | Додати термін у словник |
| `export_transcript` | `session_or_file`, `format` (srt/txt/md), `profile?` | Експорт розшифровки сесії наради або файлу |
| `generate_protocol` | `session`, `save?` | Структурований протокол наради (Підсумок/Рішення/Задачі/Розділи) локальною LLM |

Помилки повертаються як структурована відповідь (`isError: true` з поясненням
українською), а не як краш сервера. Бізнес-логіка спільна з CLI
(`fronts/cli.py`) — MCP лише тонка обгортка.

## Запуск (dev)

```
python -m whisper_core.mcp_server
```

Сервер читає JSON-RPC 2.0 повідомлення по одному на рядок зі stdin і пише
відповіді у stdout (потоки перевлаштовуються на UTF-8 — обовʼязково для укр. тексту
на Windows). Ручна перевірка:

```
printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize"}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' \
  | python -m whisper_core.mcp_server
```

## Підключення до Claude Desktop

Додайте до файлу конфігурації Claude Desktop
(`%APPDATA%\Claude\claude_desktop_config.json` на Windows,
`~/Library/Application Support/Claude/claude_desktop_config.json` на macOS):

```json
{
  "mcpServers": {
    "balachky": {
      "command": "C:\\Path\\To\\Balachky\\.venv\\Scripts\\python.exe",
      "args": ["-m", "whisper_core.mcp_server"],
      "cwd": "C:\\Path\\To\\Balachky"
    }
  }
}
```

`command` — інтерпретатор Python середовища застосунку; `cwd` — корінь репозиторію
(щоб імпортувалися пакети `whisper_core` і `fronts`). Перезапустіть Claude Desktop —
інструменти зʼявляться в переліку.

## Frozen-білд (PyInstaller)

У замороженому застосунку `python -m whisper_core.mcp_server` **не працює** (немає
інтерпретатора з `-m`). Потрібна окрема exe-точка входу, що викликає
`whisper_core.mcp_server.main()`, і в конфізі Claude Desktop `command` вказує на цей
exe без `args`. Хук наразі не реалізовано — для локального тестування достатньо
dev-режиму вище.

## Безпека

- Транспорт лише stdio — сервер не відкриває мережевих портів і нічого не слухає.
- Інструменти працюють з файлами **лише в межах даних застосунку** (профілі, теки
  нарад). Цільовий шлях (`session`/`session_or_file`/`profile`) резолвиться і
  звіряється через `paths.safe_under(root, target)` перед будь-яким читанням чи
  записом. Спроба traversal (`../../…`, абсолютний шлях поза даними, профіль
  `../evil`) відхиляється структурованою помилкою «Шлях поза межами даних
  застосунку», файл за межами НЕ читається. Це критично тому, що MCP-сервером
  керує ШІ-агент під потенційно недовіреним текстовим контекстом (промт-інʼєкція).
- `generate_protocol` вимагає локально завантаженої моделі протоколу; за її
  відсутності повертає чесну структуровану помилку, а не заглушку.
