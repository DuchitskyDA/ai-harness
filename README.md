# Оснастка для работы с Claude Code и Codex

An open scaffold for running software work through AI agents: Claude Code plus Codex CLI,
with a written policy, agent roles, hooks, an installer and a cross-model review loop.
It is a starting point, not a finished product: expect to adapt it to yourself and to each project.

Это открытый каркас, а не готовый продукт. Он описывает и частично автоматизирует один
способ работы с агентами, но не настроен под ваши проекты и привычки. Свод правил, политика
разрешений, роли и модели, проверки и пресеты рассчитаны на то, что вы их перепишете — под себя
и под каждый проект отдельно. Тексты правил и скиллов написаны по-русски.

## Процесс

1. **Спецификация.** До первой правки — спека файлом: было → будет для пользователя, файлы,
   побочные эффекты, критерий и команда проверки. Работа больше одного исполнителя — эпик:
   грилл решений с владельцем (`grill-with-docs`), нарезка на тикеты по волнам (`to-tickets`),
   цепочка — скилл `flow`.
2. **Реализация агентом.** Тикет выполняет субагент Claude (`worker` или `mechanical`) в своей
   worktree: лёгкий тест red → green, затем правка; проверяет только затронутое.
3. **Ревью другой моделью.** Дифф тикета ревьюит Codex в роли `adversarial`, до трёх раундов;
   исполнитель правит находки или отклоняет их с доказательством.
4. **Независимая приёмка.** Итог эпика принимает отдельный прогон Codex `adversarial` по
   критериям приёмки; изменения идут через PR, сливает человек.

Продуктовые решения принимает владелец — человек, который ведёт работу. Агент формулирует
продуктовую дельту (было → будет → последствия) и ждёт ответа; технические решения принимает сам
и перечисляет в отчёте. Подробно — `harness/POLICY.md`.

## Состав

| Часть | Где | Что делает |
|---|---|---|
| Свод и политика | `harness/DIGEST.md`, `harness/DIGEST.codex.md`, `harness/POLICY.md` | короткий свод для `~/.claude/CLAUDE.md` и `~/.codex/AGENTS.md`; полная политика процесса, разрешений, ролей и PR |
| Роли Claude | `claude/agents/` | `worker`, `mechanical`, `auditor`, `researcher`: модель, effort и системный промпт |
| Роли Codex | `harness/profiles/codex/` | `adversarial` (ревью и приёмка), `ops` (серверы) |
| Обёртка Codex | `harness/codex.py` | запуск роли Codex по профилю с таймаутом, sandbox и файлом ответа |
| Скиллы | `claude/skills/` | `flow`, `grill-with-docs`, `to-tickets`, `ub-mode`, `writing-for-agents`, `retro` |
| Политика прав | `claude/settings.example.json`, `harness/permit.py` | шаблон settings: auto mode Claude Code, правила владельца в `autoMode.soft_deny`; `permit.py` добавляет строку в `autoMode.allow` после явного «да» |
| Хуки | `harness/hooks/` | SessionStart, Notification, Stop, UserPromptSubmit, PostToolUse, SessionEnd: карточка сессии, окно уведомления, подсказки после `git push` и PR, уборка контейнеров сессии; вызовы не блокируют |
| Установщик | `install.sh`, `install.ps1`, `harness/install.py`, `harness/validate_install.py` | план без записи, установка с бэкапом и манифестом, read-only сверка |
| Проверки проектов | `harness/presets/` | локальный pre-commit: lint тронутых файлов, форматтеры, typecheck; Python и TS-пресеты |
| PR и CI | `harness/pr_assets.py`, `harness/ci_watch.py` | снимки в теле PR и проверка тела; ожидание GitHub Actions после пуша |
| Строка статуса | `claude/statusline-command.sh` | модель, контекст, лимиты, состояние сессии |

Тесты лежат рядом с кодом (`test_*.py`), CI — `.github/workflows/harness-tests.yml`.

## Что настроить под себя

- **Свод и политика** — `harness/DIGEST*.md` и `harness/POLICY.md`: язык ответов, форма отчёта,
  правила туннеля и установки, процесс. `claude/CLAUDE.md` — побайтная копия `harness/DIGEST.md`
  (это проверяет тест): правьте DIGEST и копируйте.
- **Разрешения** — `autoMode` в `claude/settings.example.json`. Классификатор auto mode
  вероятностный: правило может не сработать, детерминированного запрета нет.
- **Роли и модели** — frontmatter в `claude/agents/*.md`, `harness/profiles/codex/*.toml` и таблица
  «Роли и модели» в POLICY. В них названы конкретные модели; недоступную замените.
- **Проектные исключения** — `my-app` в своде, POLICY и settings — пример проекта с защищёнными
  ветками и CI. Замените своим проектом или уберите.
- **Пресеты проверок** — `harness/presets/projects/`: поставляются только `default.json` и
  `harness.json` (сама оснастка); config своего проекта добавляется рядом, формат — `harness/presets/README.md`.
- **Снимки PR** — `REPO` в `harness/pr_assets.py` (`my-user/pr-assets`) замените своим приватным репозиторием.

## Установка

```bash
git clone <url вашей копии> ~/claude-config-repo
cd ~/claude-config-repo
python3 harness/install.py      # план: что будет сделано, без записи
./install.sh                    # установка: python3 -X utf8 -B harness/install.py --apply
python3 -X utf8 -B ~/.harness/validate_install.py
```

На Windows — `python` вместо `python3` и `install.ps1` вместо `install.sh`.

Установщик кладёт свод, роли, скиллы и settings в `~/.claude`, `~/.codex` и `~/.agents/skills`,
делает `~/.harness` ссылкой на `harness/` клона и сливает `~/.claude/settings.json` с шаблоном.
Всё изменённое сохраняется в `~/.codex/backups/harness-install-<UTC>-<id>/` с `manifest.json`;
повторный запуск без изменений ничего не пишет. В рабочие репозитории ничего не пишется.
Подробности и восстановление по бэкапу — `harness/README.md`.

Установка меняет ваши глобальные настройки Claude Code и Codex. Сначала прочитайте план и шаблон
`claude/settings.example.json`.

## Требования

- Windows или macOS.
- git; Python 3.11+; pytest и PyYAML для тестов.
- Claude Code CLI: установщик читает встроенные правила `claude auto-mode defaults`.
- Codex CLI для ролей `adversarial` и `ops`.
- Node.js и npm для TS-пресета pre-commit (`harness/presets/ts`).
- bash и jq для строки статуса (на Windows — bash из Git for Windows).
- gh для PR и CI на GitHub; glab — если проекты на GitLab.

## Ограничения

- Целевые ОС — Windows и macOS (правило репозитория, `CLAUDE.md`); на Linux в CI идут только тесты.
- Рассчитано на связку Claude Code и Codex CLI; другие агенты не поддерживаются.
- Часть правил описывает запуск ролей в оркестраторе T3 (`delegate_task`); вне T3 роли запускаются
  штатным `Agent` и `harness/codex.py`.
- Защита — правила auto mode Claude Code, а не песочница: они снижают риск, но не гарантируют запрет.
- Каркас не утверждает, что такой процесс даёт лучший результат, чем другой: это описание одного
  рабочего способа, которое нужно проверить и подстроить на своих задачах.
