# Глобальная оснастка

Каталог `harness/` этого репозитория разворачивается как `~/.harness` — не
копией, а ссылкой на клон (её создаёт установщик): через `~/.harness`
адресуются собственные хуки и общий свод. Подключается к Claude Code общий
процесс работы: готовые роли и скиллы, эпики — файлами `epic.md` в каталоге
спек. Реализация идёт через
`Agent(worker)` (Opus 5.5 low: выполнить согласованную спеку и проверить изменённую
область) или `Agent(mechanical)` (Sonnet 5.5 low: решение дано готовым образцом,
остаётся однотипная правка); роли — `claude/agents/`.

Состав: `install.py` — единственный установщик (бэкап с манифестом,
`install_files.py`, `hook_spec.py`, `install_repo.py`),
`validate_install.py` — read-only сверка, `hooks/` — четыре скрипта на шести событиях
(SessionStart, Notification, Stop, UserPromptSubmit, SessionEnd), `profiles/codex/` и
`codex.py` — роли Codex и их запуск, `DIGEST*.md` и `POLICY.md` — свод и политика,
`presets/` — локальный pre-commit проектов (свой README). Защита — штатный auto mode
Claude Code: правила владельца в `autoMode.soft_deny` шаблона, в `hard_deny` одна
заглушка, поэтому любой запрет снимается «да» владельца.

Ниже — установка на macOS. На Windows шаги те же; различия названы отдельно в
конце каждого раздела.

## 1. Предпосылки

| Что | Зачем | Проверка |
|---|---|---|
| git | worktree для исполнителя, ветки и PR | `git --version` |
| Python 3.11+ | `install.py` читает `tomllib`, он появился в 3.11 | `python3 --version` |
| Claude Code CLI | сам хост; установщик берёт из `claude auto-mode defaults` встроенные правила | `claude --version` |
| Node.js + npm | TS-пресет pre-commit (`presets/ts`) и его тесты | `node --version` |
| pytest | тесты оснастки | `python3 -m pytest --version` |

Хуки прописываются под ОС: на Windows `python`, на
macOS `python3`, поэтому отдельный `python` на macOS не нужен.

## 2. Установка оснастки

Клон может лежать где угодно, в том числе вне домашнего каталога (не в
`~/.harness` — это ссылка, а не клон). Установка — один вызов из корня клона:

```bash
git clone <url вашей копии репозитория> ~/claude-config-repo
cd ~/claude-config-repo
python3 harness/install.py      # план: печатает, что будет сделано, ничего не пишет
./install.sh                    # то же, что python3 -X utf8 -B harness/install.py --apply
```

Первым шагом `--apply` проверяет всё, что может отказать (встроенные правила
`claude auto-mode defaults`, типы в `settings.json`, конфликты скиллов, цели
записи), и до этого ничего не пишет. Затем создаёт `~/.harness` как ссылку на
`harness/` клона (symlink; на Windows без права на symlink — junction, а
существующий junction при появлении права переводится в symlink); готовая ссылка — без изменений, чужой объект или висячая ссылка по этому пути — отказ с описанием,
чужое установщик не трогает.

Дальше: своды в `~/.claude/CLAUDE.md` и `~/.codex/AGENTS.md`, роли в
`~/.claude/agents` и `~/.codex/agents` (прежние роли оснастки снимаются), скиллы
копиями в `~/.agents/skills` со ссылкой `~/.claude/skills`, ссылки памяти
проектов, слияние `~/.claude/settings.json` с собственными хуками и снятие
прежних хуков оснастки там и в `~/.codex/hooks.json`, снятие локального
`core.hooksPath` клона. В рабочие репозитории ничего не пишется.

Перед записью всё сравнивается с желаемым; совпало — записи нет. Изменённое и
снятое кладётся в `~/.codex/backups/harness-install-<UTC>-<id>/` вместе с
`manifest.json`, путь печатается в JSON итога.

Восстановление по бэкапу. В `manifest.json` перечислены операции:

* `kind: file` или `dir` — прежнее содержимое лежит по пути `backup`; вернуть —
  скопировать его на место `path` (для каталога — удалить текущий и скопировать
  каталог целиком);
* `kind: link` — на месте `path` была ссылка на `target`; вернуть — удалить
  текущий объект и создать ссылку заново (`New-Item -ItemType SymbolicLink` на Windows,
  без Developer Mode — `-ItemType Junction`; `ln -s` на macOS);
* `kind: git-config` — `path` — клон, `values` — прежние значения локального
  `core.hooksPath`; вернуть — `git -C <клон> config --local core.hooksPath <значение>`.

Файлы, которых до запуска не было, в бэкап не попадают: вернуть — удалить их.
Автоматического отката нет. Повторный `install.py --apply` после сбоя доводит
установку без дублей.

Языковые серверы и semgrep выключены в шаблоне. Набор плагинов Claude Code —
`enabledPlugins` в `claude/settings.example.json`: плагины ставятся отдельно
(`claude plugin install <имя>@<маркетплейс>`), шаблон только включает или выключает их.

Проверка после установки:

```bash
python3 -X utf8 -B ~/.harness/validate_install.py   # JSON {"ok": true, ...}, код 0
./install.sh                                        # повтор: "status": "unchanged", "backup": null
```

`validate_install.py` только читает: сверяет установленное с текущим контрактом
`install.py`. Свежесть встроенных правил autoMode для другой версии CLI
показывает живой `claude auto-mode config`.

**Windows:** то же самое, но `python` вместо `python3` и `install.ps1` вместо
`install.sh`; путь — `C:\Users\<вы>\.harness`.

## 2a. Роли Codex

Роли Codex — `adversarial`, `ops` — запускает главная сессия, adversarial на тикет — его worker:
`python -X utf8 -B ~/.harness/codex.py --role <роль> --task <файл|-> --cwd <каталог>`
с профилями `~/.codex/agents/<роль>.toml`; синтаксис и коды выхода — `POLICY.md`,
«Роли и модели». Роли Claude — субагенты сессии (`claude/agents/`): auditor,
researcher, worker, mechanical.

## 3. Обновление

```bash
cd ~/claude-config-repo && git pull
./install.sh                    # Windows: powershell -ExecutionPolicy Bypass -File .\install.ps1
python3 -X utf8 -B harness/validate_install.py
```

## 4. Границы

Туннели владельца неприкосновенны: ни установка, ни враппер не трогают сеть,
адаптеры, маршруты, DNS и клиенты туннеля. Ключи и креденшелы в постановку
агенту не включают, а дерево агент читает сам.
