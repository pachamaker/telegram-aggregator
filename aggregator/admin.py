"""Optional Bot API administration for one forum topic."""

import asyncio
import json
import logging
import re
import sqlite3
import time
from html import escape
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .review import CONTROL_LABELS, READ_ERRORS
from .runtime import source_link
from .store import identifier

LOG = logging.getLogger(__name__)
POLL_TIMEOUT = 10
RETRY_DELAY = 5
SOURCE_PAGE_SIZE = 12
COMMAND_GROUPS = (
    ("Каналы и топики", (
        ("/sources [СТРАНИЦА]", "Список каналов и их ID"),
        ("/review", "Разобрать новые каналы из папки"),
        ("/retopic", "Сменить топик кнопками"),
        ("/route ID ID_ТОПИКА", "Перенаправить канал по ID топика"),
    )),
    ("Публикации", (
        ("/pause", "Приостановить или возобновить пересылку"),
        ("/reactions", "Выбрать, как фильтровать посты по реакциям"),
        ("/age", "Выбрать, через сколько часов считать реакции"),
    )),
    ("Справка и диагностика", (
        ("/settings", "Открыть это меню"),
        ("/skips [ID]", "Показать причины последних пропусков"),
    )),
)
USAGE = {
    syntax.split()[0]: syntax
    for _, commands in COMMAND_GROUPS
    for syntax, _ in commands
}
# Keep previously used text commands available without adding them to the menu.
USAGE.update({"/source": "/source ID", "/resume": "/resume ID confirm"})


class BotAPIError(Exception):
    def __init__(self, code=503, retry_after=0, *, method=None, detail=None):
        self.code = code
        self.retry_after = retry_after
        summary = detail or f"Bot API {code}"
        super().__init__(f"{method}: {summary}" if method else summary)


def _bot_request(token, method, data):
    request = Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=json.dumps(data).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    status = None
    try:
        try:
            response = urlopen(request, timeout=POLL_TIMEOUT + 5)
        except HTTPError as error:
            status = error.code
            response = error
        with response:
            result = json.load(response)
    except (HTTPException, URLError, OSError, ValueError) as error:
        # urllib errors and Telegram descriptions can contain the credential-bearing URL.
        cause = error.reason if isinstance(error, URLError) and isinstance(error.reason, Exception) else error
        detail = f"HTTP {status}" if status is not None else type(cause).__name__
        raise BotAPIError(status or 503, method=method, detail=detail) from None
    if not isinstance(result, dict):
        detail = f"HTTP {status}" if status is not None else "unexpected JSON response"
        raise BotAPIError(status or 503, method=method, detail=detail)
    if result.get("ok") is True and "result" in result:
        return result["result"]
    code = result.get("error_code")
    parameters = result.get("parameters")
    retry_after = parameters.get("retry_after") if isinstance(parameters, dict) else None
    raise BotAPIError(code if identifier(code) else status or 503,
                      retry_after if identifier(retry_after) else 0,
                      method=method, detail=f"HTTP {status}" if status is not None else None)


def _number(value, minimum=1, maximum=2**63 - 1):
    if value.isascii() and value.isdecimal() and len(value) <= 19:
        number = int(value)
        if minimum <= number <= maximum:
            return number
    raise ValueError


class AdminBot:
    def __init__(self, db, config, token, validate_topic, review=None):
        self.db = db
        self.chat_id = -1_000_000_000_000 - config["group_id"]
        self.topic_id = config["admin_topic_id"]
        self.operators = set(config.get("operator_ids", []))
        self.token = token
        self.validate_topic = validate_topic
        self.review = review
        self.username = None
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS admin_changes (
                update_id INTEGER PRIMARY KEY,
                operator_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                command TEXT NOT NULL,
                changed_at INTEGER NOT NULL,
                previous TEXT NOT NULL,
                current TEXT NOT NULL
            )
        """)

    async def api(self, method, **data):
        return await asyncio.to_thread(_bot_request, self.token, method, data)

    async def reply(self, text):
        # Keep messages below the Bot API limit even when source titles contain emoji.
        while text:
            end = len(text) if len(text) <= 1900 else text.rfind("\n", 0, 1900)
            end = end if end > 0 else 1900
            try:
                await self.api("sendMessage", chat_id=self.chat_id,
                               message_thread_id=self.topic_id, text=text[:end])
            except BotAPIError as error:
                LOG.warning("Telegram admin reply failed (%s)", error)
                return
            text = text[end:].lstrip("\n")

    def _operator(self, message, sender):
        if not isinstance(message, dict):
            return
        chat = message.get("chat")
        if not isinstance(chat, dict) or not isinstance(sender, dict):
            return
        operator = sender.get("id")
        if (
            chat.get("id") != self.chat_id
            or chat.get("type") != "supergroup"
            or chat.get("is_forum") is not True
            or message.get("message_thread_id") != self.topic_id
            or message.get("is_topic_message") is not True
            or not identifier(operator)
            or operator not in self.operators
            or sender.get("is_bot") is not False
            or any(field in message for field in (
                "sender_chat", "forward_origin", "forward_date", "via_bot", "sender_business_bot",
            ))
            or message.get("is_automatic_forward")
        ):
            return
        return operator

    async def _is_admin(self, operator):
        try:
            member = await self.api("getChatMember", chat_id=self.chat_id, user_id=operator)
        except BotAPIError as error:
            if error.code not in (400, 403):
                raise
            return None
        if not isinstance(member, dict):
            raise BotAPIError(502, method="getChatMember", detail="unexpected response")
        user = member.get("user")
        return (member.get("status") in ("creator", "administrator")
                and isinstance(user, dict) and user.get("id") == operator)

    async def _callback(self, update_id, query):
        message = query.get("message")
        operator = self._operator(message, query.get("from"))
        data = query.get("data")
        if not operator or not isinstance(query.get("id"), str) or not isinstance(data, str):
            return
        menu, _, page_value = data.partition(":")
        show_sources = menu in ("sources", "sources-ids")
        review = self.review is not None and data.startswith("review:")
        if not (show_sources or review) or not await self._is_admin(operator):
            return
        try:
            if review:
                await self.review.click(self, operator, update_id, query)
                return
            message_id = message.get("message_id")
            if not identifier(message_id):
                return
            try:
                page = _number(page_value, minimum=0)
            except ValueError:
                return
            await self.api("answerCallbackQuery", callback_query_id=query["id"])
            await self._show_settings(page, message_id=message_id, ids=menu == "sources-ids")
        except BotAPIError as error:
            if show_sources and error.code in (400, 403):
                LOG.warning("Не удалось открыть страницу источников (%s)", error)
            elif error.code == 400:
                await self.reply("Карточка недоступна. Откройте новую командой /review.")
            else:
                raise

    async def handle_update(self, update):
        update_id = update.get("update_id")
        if not identifier(update_id):
            return
        query = update.get("callback_query")
        if isinstance(query, dict):
            await self._callback(update_id, query)
            return
        message = update.get("message")
        if not isinstance(message, dict):
            return
        operator = self._operator(message, message.get("from"))
        text = message.get("text")
        if not operator or not isinstance(text, str):
            return
        if not text.startswith("/"):
            if self.review is not None:
                pending = self.review.pending_input(operator, message)
                if pending is not None and not self._processed(update_id) and await self._is_admin(operator):
                    await self.review.apply_input(self, operator, update_id, *pending, text)
            return

        addressed, *args = text.split()
        command, separator, username = addressed.partition("@")
        entities = message.get("entities", [])
        if command not in USAGE or not isinstance(entities, list) or not any(
            isinstance(entity, dict) and entity.get("type") == "bot_command"
            and entity.get("offset") == 0 and entity.get("length") == len(addressed)
            for entity in entities
        ):
            return
        if separator:
            if self.username is None:
                bot = await self.api("getMe")
                username_value = bot.get("username") if isinstance(bot, dict) else None
                if not isinstance(username_value, str) or not username_value:
                    raise BotAPIError(502, method="getMe", detail="unexpected response")
                self.username = username_value
            if username.casefold() != self.username.casefold():
                return
        if self._processed(update_id):
            return

        is_admin = await self._is_admin(operator)
        if is_admin is None:
            await self.reply("Не удалось проверить роль в форуме. Проверьте права бота.")
            return
        if not is_admin:
            await self.reply("Команда доступна владельцу и администраторам форума.")
            return
        if command == "/settings" and not args:
            await self._show_settings()
            return
        if command == "/sources" and len(args) <= 1:
            try:
                page = _number(args[0]) if args else 1
            except ValueError:
                await self.reply(f"Формат команды\n{USAGE[command]}")
                return
            await self._show_settings(page)
            return
        if command in CONTROL_LABELS and not args and self.review is not None:
            try:
                await self.review.show_controls(self, operator, command)
            except BotAPIError as error:
                if error.code not in (400, 403):
                    raise
                LOG.warning("Не удалось показать настройки канала (%s)", error)
            return
        if command in ("/review", "/retopic") and self.review is not None:
            try:
                channel_id = _number(args[0]) if args else None
                if len(args) > 1:
                    raise ValueError
            except ValueError:
                await self.reply(USAGE[command])
                return
            try:
                if command == "/review":
                    await self.review.show(self, operator, channel_id=channel_id)
                elif channel_id is None:
                    await self.review.show_channels(self, operator)
                else:
                    await self.review.show_edit(self, operator, channel_id)
            except BotAPIError as error:
                if error.code not in (400, 403):
                    raise
                LOG.warning("Не удалось показать карточку разбора (%s)", error)
            return

        await self.reply(await self._command(command, args, operator, update_id))

    def _processed(self, update_id):
        return self.db.execute(
            "SELECT 1 FROM admin_changes WHERE update_id = ?", (update_id,)
        ).fetchone() is not None

    async def _command(self, command, args, operator, update_id):
        try:
            if command == "/skips" and len(args) <= 1:
                return self._skips(_number(args[0]) if args else None)
            if not args:
                raise ValueError
            channel_id = _number(args[0])
            if command == "/source" and len(args) == 1:
                source = self._get_source(channel_id)
                if source is None:
                    return f"Источник {channel_id} не найден."
                return self._source(source) + "\n\n" + self._skips(channel_id)

            if command == "/route" and len(args) == 2:
                topic = _number(args[1], maximum=2**31 - 1)
                changes = {"topic_id": topic}
            elif command == "/pause" and len(args) == 1:
                changes = {"enabled": 0}
            elif command == "/resume" and args[1:] in ([], ["confirm"]):
                changes = {"enabled": 1}
            elif command == "/reactions" and args[1:] in (["off"], ["auto"]):
                changes = {"reaction_mode": args[1], "threshold": None}
            elif command == "/reactions" and len(args) == 3 and args[1] == "manual":
                changes = {"reaction_mode": "manual", "threshold": _number(args[2], minimum=0)}
            elif command == "/age" and len(args) == 2:
                changes = {"reaction_hours": _number(args[1], maximum=2**31 - 1)}
            else:
                raise ValueError
        except ValueError:
            return f"Формат команды\n{USAGE[command]}"

        source = self._get_source(channel_id)
        if source is None:
            return f"Источник {channel_id} не найден."
        if command == "/route":
            try:
                is_open = await self.validate_topic(topic)
            except Exception:
                LOG.warning("Telegram admin topic validation failed")
                return f"Не удалось проверить тему {topic}. Повторите команду."
            if not is_open:
                return f"Тема {topic} недоступна или закрыта."
        if command == "/resume":
            if not source["topic_id"]:
                return f"Сначала назначьте тему командой /route {channel_id} ID_ТОПИКА."
            if not source["enabled"] and len(args) == 1:
                return ("Возобновление пропустит публикации до следующего чтения источника. "
                        f"Для подтверждения /resume {channel_id} confirm")
        return self._change_source(command, operator, update_id, channel_id, changes)

    def _get_source(self, channel_id):
        return self.db.execute("SELECT * FROM sources WHERE channel_id = ?", (channel_id,)).fetchone()

    async def _show_settings(self, page=0, *, message_id=None, ids=False):
        formatting = {}
        if page:
            topics = {}
            if self.review is not None:
                try:
                    topics = dict(await self.review.list_topics())
                except READ_ERRORS as error:
                    LOG.warning("Не удалось загрузить названия топиков (%s)", type(error).__name__)
            text, pages = self._sources(page, topics, ids=ids)
            formatting = {"parse_mode": "HTML", "link_preview_options": {"is_disabled": True}}
            prefix = "sources-ids" if ids else "sources"
            previous = min(page - 1, pages)
            back = f"{prefix}:{previous}" if previous else "sources:0"
            buttons = [{"text": "Назад", "callback_data": back}]
            if page < pages:
                buttons.append({"text": "Дальше", "callback_data": f"{prefix}:{page + 1}"})
            rows = [buttons]
            if page <= pages:
                rows.append([{"text": "Скрыть ID" if ids else "Показать ID",
                              "callback_data": f"{'sources' if ids else 'sources-ids'}:{page}"}])
        else:
            sections = ["Настройки подборки"]
            for title, commands in COMMAND_GROUPS:
                lines = [f"{syntax} — {description}" for syntax, description in commands]
                sections.append("\n".join([title, *lines]))
            text = "\n\n".join(sections)
            rows = [[{"text": "Источники", "callback_data": "sources:1"}]]
        markup = {"inline_keyboard": rows}
        if message_id is None:
            try:
                await self.api("sendMessage", chat_id=self.chat_id, message_thread_id=self.topic_id,
                               text=text, reply_markup=markup, **formatting)
            except BotAPIError as error:
                LOG.warning("Telegram admin reply failed (%s)", error)
        else:
            await self.api("editMessageText", chat_id=self.chat_id, message_id=message_id,
                           text=text, reply_markup=markup, **formatting)

    def _sources(self, page, topics, *, ids=False):
        count = self.db.execute("SELECT count(*) FROM sources").fetchone()[0]
        if not count:
            return "Источников пока нет.", 0
        pages = (count + SOURCE_PAGE_SIZE - 1) // SOURCE_PAGE_SIZE
        if page > pages:
            return f"Страницы {page} нет. Доступные страницы — от 1 до {pages}.", pages
        rows = self.db.execute("""
            SELECT s.channel_id, s.title, s.username, s.topic_id, c.last_id FROM sources s
            LEFT JOIN cursors c USING (channel_id)
            ORDER BY s.category, s.title, s.channel_id LIMIT ? OFFSET ?
        """, (SOURCE_PAGE_SIZE, (page - 1) * SOURCE_PAGE_SIZE))
        lines = [f"Источники · страница {page} из {pages}", ""]
        for source in rows:
            title = escape(" ".join(source["title"].split())[:64])
            username = (source["username"] or "").lstrip("@")
            if re.fullmatch(r"[a-zA-Z0-9_]+", username):
                title = f'<a href="https://t.me/{username}">{title}</a>'
            elif identifier(source["last_id"]) and not username:
                title = f'<a href="{source_link(source, source["last_id"])}">{title}</a>'
            topic_id = source["topic_id"]
            topic = topics.get(topic_id, "топик недоступен") if topic_id else "без топика"
            line = f"{title} · {escape(topic[:64])}"
            if ids:
                line += f"\nID канала <code>{source['channel_id']}</code>"
                if topic_id:
                    line += f" · ID топика <code>{topic_id}</code>"
            lines.append(line)
        hint = ("ID канала нужен для /route и /skips. ID топика — второй аргумент /route."
                if ids else "Нужен номер для команды? Нажмите «Показать ID».")
        lines.extend(["", hint])
        return "\n".join(lines), pages

    def _source(self, source):
        mode = source["reaction_mode"]
        threshold = source["threshold"]
        calibration = ""
        if mode == "auto":
            baseline = self.db.execute("""
                SELECT threshold, sample_size FROM baselines WHERE channel_id = ? AND hours = ?
            """, (source["channel_id"], source["reaction_hours"])).fetchone()
            threshold = baseline["threshold"] if baseline else None
            calibration = f", выборка {baseline['sample_size']}" if baseline else ", калибровка"
        status = "активен" if source["enabled"] else "на паузе"
        title = " ".join(source["title"].splitlines())
        return (f"{source['channel_id']} · {title} · {source['category']} · {status} · "
                f"тема {source['topic_id'] or 'не задана'} · реакции {mode}, "
                f"порог {threshold if threshold is not None else '—'}{calibration}, "
                f"замер {source['reaction_hours']} ч · политика {source['policy']}")

    def _skips(self, channel_id):
        rows = self.db.execute("""
            SELECT channel_id, post_ids, reason, shadow FROM decisions
            WHERE (? IS NULL OR channel_id = ?) AND reason <> 'published'
            ORDER BY id DESC LIMIT 5
        """, (channel_id, channel_id))
        lines = [f"{row['channel_id']} · {row['post_ids']} · {row['reason']}"
                 + (" (shadow)" if row["shadow"] else "") for row in rows]
        return "Последние пропуски\n" + ("\n".join(lines) or "Нет записей.")

    def _change_source(self, command, operator, update_id, channel_id, changes):
        now = int(time.time())
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            if self._processed(update_id):
                return "Команда уже обработана."
            source = self._get_source(channel_id)
            if source is None:
                return f"Источник {channel_id} не найден."
            previous = {key: source[key] for key in changes}
            skipped = 0
            if previous != changes:
                assignments = ", ".join(f"{key} = ?" for key in changes)
                self.db.execute(f"UPDATE sources SET {assignments} WHERE channel_id = ?",
                                (*changes.values(), channel_id))
                if command == "/pause":
                    self.db.execute("""
                        INSERT INTO decisions(channel_id, post_ids, reason, details, decided_at, shadow)
                        SELECT channel_id, post_ids, 'paused_by_operator', ?, ?, lane = 'shadow'
                        FROM queue WHERE channel_id = ? AND status = 'pending'
                    """, (json.dumps({"operator_id": operator}), now, channel_id))
                    skipped = self.db.execute("""
                        UPDATE queue SET status = 'skipped' WHERE channel_id = ? AND status = 'pending'
                    """, (channel_id,)).rowcount
                    self.db.execute("""
                        UPDATE queue SET cancel_retry = 1 WHERE channel_id = ? AND status = 'sending'
                    """, (channel_id,))
                if command == "/resume":
                    self.db.execute("DELETE FROM cursors WHERE channel_id = ?", (channel_id,))
                if command in ("/age", "/reactions"):
                    self.db.execute("""
                        UPDATE queue SET due = min(due, ?)
                        WHERE channel_id=? AND status='pending' AND policy='v2'
                    """, (now, channel_id))
                if command == "/age":
                    hours = changes["reaction_hours"]
                    self.db.execute("DELETE FROM baselines WHERE channel_id=?", (channel_id,))
                    self.db.execute("DELETE FROM measurements WHERE channel_id=?", (channel_id,))
                    if source["reaction_mode"] != "off":
                        self.db.execute("""
                            INSERT OR IGNORE INTO measurements(channel_id,post_ids,due,hours)
                            SELECT channel_id,post_ids,published_at + ? * 3600,?
                            FROM queue WHERE channel_id=? AND status='pending' AND policy='v2'
                        """, (hours, hours, channel_id))
            self.db.execute("""
                INSERT INTO admin_changes(update_id, operator_id, channel_id, command, changed_at, previous, current)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (update_id, operator, channel_id, command, now, json.dumps(previous), json.dumps(changes)))
        if previous == changes:
            return "Эта настройка уже действует."
        response = self._source(self._get_source(channel_id))
        if command == "/pause":
            response += f"\nПропущено записей очереди {skipped}. Начатые отправки сохраняют своё состояние."
        if command == "/resume":
            response += "\nПри следующем чтении курсор будет установлен на текущую публикацию."
        return response


async def run_admin(db, config, token, validate_topic, review=None):
    """Long poll in the runtime event loop and keep all SQLite work on that loop."""
    if not token or not config.get("admin_topic_id") or not config.get("operator_ids"):
        return
    bot = AdminBot(db, config, token, validate_topic, review)
    offset = None
    while True:
        try:
            params = {"timeout": POLL_TIMEOUT, "allowed_updates": ["message", "callback_query"]}
            if offset is not None:
                params["offset"] = offset
            updates = await bot.api("getUpdates", **params)
            if not isinstance(updates, list) or any(
                not isinstance(update, dict) or not identifier(update.get("update_id"))
                for update in updates
            ):
                raise BotAPIError(502, method="getUpdates", detail="unexpected response")
            if not updates:
                # Telegram confirmed the previous batch. Drop the offset so it can reset after idle weeks.
                offset = None
            for update in updates:
                await bot.handle_update(update)
                offset = update["update_id"] + 1
        except BotAPIError as error:
            LOG.warning("Telegram admin request failed (%s). Retrying", error)
            await asyncio.sleep(max(RETRY_DELAY, error.retry_after))
        except sqlite3.OperationalError:
            LOG.warning("Telegram admin database operation failed. Retrying")
            await asyncio.sleep(RETRY_DELAY)
