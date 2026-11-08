# Перенос Telegram Aggregator на VPS

Эта инструкция переносит уже настроенный экземпляр. На сервере будут использоваться те же папка, форум и бот. Каталог состояния сохраняет настройки каналов, очередь, позиции чтения и историю отправок.

Для нового экземпляра пройди [запуск с нуля](../README.md#запуск-с-нуля). Для старой базы v1 используй [отдельный порядок миграции](#переход-с-v1).

## 1. Подготовь сервер и код

Нужны SSH, Docker Engine и Docker Compose v2. Примеры рассчитаны на Linux и непривилегированного пользователя с доступом к Docker. Выбери для v2 отдельный каталог, чтобы исходный экземпляр и возможный старый v1 можно было восстановить независимо.

На VPS, если исходники уже опубликованы:

```sh
git clone https://github.com/pachamaker/telegram-aggregator.git ~/telegram-aggregator-app
cd ~/telegram-aggregator-app
docker compose version
docker compose build aggregator
```

До публикации можно передать только подготовленные файлы Git. Запусти это из чистого локального репозитория, где исходники уже добавлены в индекс Git. Замени `user@vps.example` на SSH-адрес.

```sh
VPS_HOST=user@vps.example
release_dir="$(mktemp -d)"
git checkout-index --all --prefix="$release_dir/"
tar -C "$release_dir" -czf - . |
  ssh "$VPS_HOST" 'test ! -e "$HOME/telegram-aggregator-app/compose.yaml" &&
    mkdir -p "$HOME/telegram-aggregator-app" &&
    tar -xzf - -C "$HOME/telegram-aggregator-app"'
```

После передачи собери образ командой `docker compose build aggregator` на VPS из нового каталога.

## 2. Сделай закрытую копию состояния

Останови агрегатор там, где он работал. Для запуска в терминале нажми `Ctrl+C` и дождись выхода. Для Docker используй `docker compose stop aggregator`. Если тот же форум обслуживает v1, останови и его перед включением нового экземпляра.

Из каталога работающего ранее экземпляра создай архив. Это каталог с реальным `config.json` и `state/`, где лежат `aggregator-v2.sqlite3`, `telegram.session` и `credentials.json`. Копируй весь каталог состояния после остановки, включая служебные файлы SQLite, если они есть.

```sh
VPS_HOST=user@vps.example
umask 077
backup_dir="$(mktemp -d)"
tar -czf "$backup_dir/telegram-aggregator-state.tar.gz" \
  --exclude='state/v2.lock' config.json state
scp -p "$backup_dir/telegram-aggregator-state.tar.gz" \
  "$VPS_HOST:telegram-aggregator-state.tar.gz"
```

Архив содержит Telegram-сессию и ключи. Храни его в закрытом каталоге и передавай по SSH. Токен бота задай отдельно на сервере.

## 3. Восстанови данные и настрой окружение

На VPS открой новый каталог. Следующая команда извлечёт данные только если в нём ещё нет `state/` и `config.json`.

```sh
cd ~/telegram-aggregator-app
umask 077
test ! -e state && test ! -e config.json &&
  tar -xzf "$HOME/telegram-aggregator-state.tar.gz"
chmod 700 state
chmod 600 config.json state/aggregator-v2.sqlite3 state/telegram.session state/credentials.json
```

В Bash введи токен, когда появится приглашение. Если бот не нужен, нажми Enter. Команды ниже запишут значения в новый `.env`. При наличии `.env` отредактируй его вручную.

```bash
read -r -s -p 'Токен бота: ' bot_token
printf '\n'
(
  umask 077
  set -C
  printf 'AGGREGATOR_UID=%s\nAGGREGATOR_GID=%s\nAGGREGATOR_BOT_TOKEN=%s\n' \
    "$(id -u)" "$(id -g)" "$bot_token" > .env
)
unset bot_token
```

Если администрируешь сервер из-под root, укажи в `.env` UID и GID выделенного непривилегированного пользователя. Назначь ему владельца `config.json` и `state/` с помощью `chown -R UID:GID config.json state`, подставив числа. `.env` может принадлежать пользователю, который запускает Compose. Сохрани права `600` для `.env` и приватных файлов и `700` для `state/`.

## 4. Проверь и запусти

Для перенесённого v2 повторный `auth`, `init` и `migrate-v1` обычно не нужны. `check` проверяет доступ пользовательской сессии к форуму, источникам и открытым темам. Запускай его до старта контейнера, поскольку сервис и проверка используют одну блокировку состояния.

```sh
docker compose config --quiet &&
  docker compose run --rm aggregator check &&
  docker compose up -d aggregator
docker compose ps aggregator
docker compose logs --tail=80 aggregator
```

Проверь ответ на `/settings@имя_бота` в теме управления, если бот подключён. Затем дождись строки «Публикация» и открой указанный топик в Telegram. Проверь, что локальная копия остаётся остановленной. Успех переноса — новый пост пришёл в ожидаемую тему, а прежняя разметка сохранилась.

## Обновление и откат

- Перед обновлением останови контейнер и сделай закрытую копию `config.json` и `state/`. Обнови исходники, пересобери образ, выполни `check` и снова запусти сервис.
- Если VPS ещё не обрабатывал очередь, останови контейнер и вернись к исходному экземпляру. Если обработка уже началась, перенеси актуальный `state/` с остановленного VPS обратно или сначала сверь отправки и курсоры. Старый снимок после новых публикаций может привести к повтору.
- Если в журнале есть отправки, требующие сверки, останови сервис и выполни `docker compose run --rm aggregator pending`. После проверки факта доставки используй `reconcile ID --message-ids …` или `reconcile ID --retry-confirmed-absent`, если сообщение точно отсутствует. `ID` здесь означает ID записи очереди.

## Переход с v1

Для перехода со старой базы останови v1, скопируй её БД и конфиг и используй новый каталог состояния. Подготовь пользовательскую сессию под аккаунтом с доступом к общей папке и тому же форуму. Пример прямого запуска из корня проекта после установки зависимостей:

```sh
.venv/bin/python -m aggregator migrate-v1 --legacy-db /path/to/v1-copy.sqlite3 --legacy-config /path/to/v1-config.json --preview
.venv/bin/python -m aggregator migrate-v1 --legacy-db /path/to/v1-copy.sqlite3 --legacy-config /path/to/v1-config.json
.venv/bin/python -m aggregator check
```

Сверь число курсоров и записей очереди с предварительным отчётом. Источники после импорта работают по правилам v1. Для поэтапного переключения при остановленном сервисе используй `python -m aggregator policy ID shadow`, затем `python -m aggregator policy ID v2` для новых записей. Здесь `ID` — ID канала. В режиме сравнения Telegram получает публикации по v1, а решения v2 видны в журнале.
