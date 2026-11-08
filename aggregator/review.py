"""Inline cards for assigning and editing Telegram routes."""
import asyncio
import json
import logging
import secrets

from telethon import errors

from .setup import EXCLUDED_CATEGORY, PENDING_CATEGORY, _normalize, _visible
from .store import identifier

LOG = logging.getLogger(__name__)
PAGE_SIZE = 8
READ_ERRORS = (ValueError, OSError, asyncio.TimeoutError, errors.RPCError)
EDIT_BUTTON = ("Изменить топик каналу", "channels", 0)
HOME_BUTTON = ("← Разбор новых каналов", "home", None)
CONTROL_LABELS = {
    "/pause": "Пауза и возобновление",
    "/reactions": "Фильтр реакций",
    "/age": "Время замера реакций",
}
REACTION_LABELS = {"off": "Выключен", "auto": "Автопорог", "manual": "Ручной порог"}
INPUT_LABELS = {"title": "Название топика", "/reactions": "Минимум реакций", "/age": "Часы до замера"}


def _page_rows(choices, page, action, previous, following, *, context=None):
    page = min(page, max(0, (len(choices) - 1) // PAGE_SIZE))
    rows = [[choice] for choice in choices[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]]
    navigation = []
    if page:
        navigation.append((previous, action, page - 1))
    if (page + 1) * PAGE_SIZE < len(choices):
        navigation.append((following, action, page + 1))
    if navigation:
        if context is not None:
            navigation = [(label, kind, [context, target]) for label, kind, target in navigation]
        rows.append(navigation)
    return rows


class Review:
    def __init__(self, db, categories, list_topics, ensure_topic, validate_source):
        self.db = db
        self.categories = tuple(categories)
        self.list_topics = list_topics
        self.ensure_topic = ensure_topic
        self.validate_source = validate_source
        db.execute("""
            CREATE TABLE IF NOT EXISTS source_review_cards (
                operator_id INTEGER PRIMARY KEY, message_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL, token TEXT NOT NULL, actions TEXT NOT NULL
            )
        """)

    def pending(self, after, channel_id):
        return self.db.execute("""
            SELECT * FROM sources WHERE topic_id IS NULL AND enabled=0 AND category<>?
                AND channel_id>? AND (? IS NULL OR channel_id=?)
            ORDER BY channel_id LIMIT 1
        """, (EXCLUDED_CATEGORY, after, channel_id, channel_id)).fetchone()

    async def show(self, bot, operator, *, after=0, page=0, channel_id=None, message_id=None):
        source = self.pending(after, channel_id)
        if source is None:
            text = "В этой очереди больше нет каналов. /review покажет оставшиеся с начала."
            await self._card(bot, operator, text, [[EDIT_BUTTON]], message_id=message_id)
            return

        all_topics = await self._topic_titles(bot)
        if all_topics is None:
            return
        suggested = source["category"]
        key = _normalize(suggested)
        choices = [(title, "topic", [topic_id, title]) for topic_id, title in all_topics.items()
                   if topic_id != bot.topic_id]
        existing = {_normalize(title) for title in all_topics.values()}
        for category in (suggested, *self.categories):
            normalized = _normalize(category)
            if normalized not in existing and category not in (PENDING_CATEGORY, EXCLUDED_CATEGORY):
                choices.append(("+ " + category, "create", category))
                existing.add(normalized)
        choices.sort(key=lambda choice: (_normalize(choice[0].removeprefix("+ ")) != key,
                                         _normalize(choice[0])))
        rows = _page_rows(choices, page, "page", "← Темы", "Ещё темы →")
        rows.extend([[("Позже", "later", None), ("Исключить", "exclude", None)], [EDIT_BUTTON]])
        title = " ".join(source["title"].split())[:150]
        remaining = self.db.execute("""
            SELECT count(*) FROM sources WHERE topic_id IS NULL AND enabled=0 AND category<>?
        """, (EXCLUDED_CATEGORY,)).fetchone()[0]
        reference = f"@{source['username']}" if source["username"] else f"ID {source['channel_id']}"
        text = (f"Канал: {title}\n{reference}\nПредложение: {suggested}\nВ очереди: {remaining}\n\n"
                "Выберите тему. Кнопка с + создаст её.")
        await self._card(bot, operator, text, rows, channel_id=source["channel_id"], message_id=message_id)

    async def _topic_titles(self, bot):
        try:
            return dict(await self.list_topics())
        except READ_ERRORS:
            await bot.reply("Не удалось прочитать темы форума. Повторите /review позже.")

    async def show_channels(self, bot, operator, *, page=0, message_id=None):
        topics = await self._topic_titles(bot)
        if topics is None:
            return
        sources = self.db.execute("""
            SELECT channel_id, title, category, topic_id FROM sources WHERE topic_id IS NOT NULL
            ORDER BY title COLLATE NOCASE, channel_id
        """)
        choices = []
        for source in sources:
            title = " ".join(source["title"].split())[:62]
            topic = topics.get(source["topic_id"], source["category"])
            choices.append((f"{title} · {topic[:35]}", "edit", source["channel_id"]))
        rows = _page_rows(choices, page, "channels", "← Каналы", "Ещё каналы →")
        rows.append([HOME_BUTTON])
        text = "Выберите канал. На каждой кнопке указан его текущий топик." if choices else (
            "Пока нет каналов с назначенным топиком."
        )
        await self._card(bot, operator, text, rows, message_id=message_id)

    async def show_edit(self, bot, operator, channel, *, page=0, message_id=None):
        source = bot._get_source(channel)
        if source is None or source["topic_id"] is None:
            await bot.reply("У канала пока нет маршрута. Назначьте его через /review.")
            return
        topics = await self._topic_titles(bot)
        if topics is None:
            return
        current = topics.get(source["topic_id"], source["category"])
        choices = [(title, "move", [topic_id, title]) for topic_id, title in topics.items()
                   if topic_id not in (source["topic_id"], bot.topic_id)]
        choices.sort(key=lambda choice: (_normalize(choice[0]), choice[2][0]))
        rows = _page_rows(choices, page, "edit_page", "← Темы", "Ещё темы →")
        rows.extend([[("Создать топик…", "title", None)], [("← К списку каналов", "channels", 0)]])
        title = " ".join(source["title"].split())[:150]
        reference = f"@{source['username']}" if source["username"] else f"ID {channel}"
        text = f"Канал: {title}\n{reference}\nТекущий топик: {current}\n\nВыберите новый топик."
        await self._card(bot, operator, text, rows, channel_id=channel, message_id=message_id)

    async def show_controls(self, bot, operator, command, *, page=0, message_id=None):
        channels = self.db.execute("""
            SELECT channel_id, title, enabled FROM sources WHERE topic_id IS NOT NULL
            ORDER BY title COLLATE NOCASE, channel_id
        """)
        choices = []
        for channel in channels:
            label = " ".join(channel["title"].split())[:76]
            if command == "/pause":
                status = "активен" if channel["enabled"] else "на паузе"
                label += f" · {status}"
            choices.append((label, "control", [command, channel["channel_id"]]))
        rows = _page_rows(choices, page, "controls", "← Назад", "Дальше →", context=command)
        rows.append([("← Настройки", "settings", None)])
        text = "Выберите канал." if choices else "Пока нет каналов с назначенным топиком."
        await self._card(bot, operator, f"{CONTROL_LABELS[command]}\n\n{text}", rows, message_id=message_id)

    async def _show_control(self, bot, operator, command, channel, *, message_id=None, saved=False):
        source = bot._get_source(channel)
        if source is None or source["topic_id"] is None:
            await bot.reply("У канала пока нет маршрута. Назначьте его через /review.")
            return
        title = " ".join(source["title"].split())[:150]
        heading = f"{CONTROL_LABELS[command]}\n{title}"
        if source["username"]:
            heading += f"\n@{source['username'].lstrip('@')}"
        if saved:
            heading += "\nНастройка сохранена."

        if command == "/pause":
            if source["enabled"]:
                text = "Канал активен. При остановке публикации в очереди будут пропущены."
                rows = [[("Приостановить пересылку", "apply_control", ["/pause"])]]
            else:
                text = "Канал на паузе. После возобновления пересылка начнётся с новых постов."
                rows = [[("Возобновить с новых постов", "apply_control", ["/resume", "confirm"])]]
        elif command == "/reactions":
            current = REACTION_LABELS.get(source["reaction_mode"], source["reaction_mode"])
            if source["reaction_mode"] == "manual":
                current += f" · {source['threshold']}"
            text = (f"Сейчас: {current}\n\n"
                    "Автопорог подбирается по прошлым постам канала. "
                    "Для ручного порога укажите минимальное число реакций. "
                    "Настройка применяется в правилах v2.")
            rows = [[("Выключить", "apply_control", [command, "off"]),
                     ("Автопорог", "apply_control", [command, "auto"])],
                    [("Ручной порог…", "input", command)]]
        elif command == "/age":
            text = (f"Сейчас: {source['reaction_hours']} ч после публикации.\n\n"
                    "Через этот срок правила v2 измеряют реакции. "
                    "При смене срока автопорог калибруется заново.")
            rows = [[(f"{hours} ч", "apply_control", [command, str(hours)]) for hours in (2, 6, 12, 24)],
                    [("Другое число…", "input", command)]]
        rows.append([("← К каналам", "controls", [command, 0])])
        await self._card(bot, operator, f"{heading}\n\n{text}", rows, channel_id=channel, message_id=message_id)

    async def _apply_control(self, bot, operator, update_id, channel, command, args, *, message_id=None):
        if bot._processed(update_id):
            return
        response = await bot._command(command, [str(channel), *args], operator, update_id)
        if not bot._processed(update_id):
            if message_id is None and response.startswith("Формат команды"):
                minimum = 1 if command == "/age" else 0
                response = f"Ответьте на исходный запрос целым числом не меньше {minimum}."
            await bot.reply(response)
            return
        if message_id is None:
            self._clear_input(operator)
        view = "/pause" if command == "/resume" else command
        await self._show_control(bot, operator, view, channel, message_id=message_id, saved=True)

    async def _card(self, bot, operator, text, rows, *, channel_id=0, message_id=None):
        token = secrets.token_hex(8)
        actions = []
        keyboard = []
        for row in rows:
            buttons = []
            for label, kind, value in row:
                buttons.append({"text": label[:100], "callback_data": f"review:{token}:{len(actions)}"})
                actions.append((kind, value))
            keyboard.append(buttons)
        payload = {"chat_id": bot.chat_id, "text": text, "reply_markup": {"inline_keyboard": keyboard}}
        if message_id is None:
            result = await bot.api("sendMessage", message_thread_id=bot.topic_id, **payload)
            message_id = result.get("message_id") if isinstance(result, dict) else None
            if not identifier(message_id):
                return
        else:
            await bot.api("editMessageText", message_id=message_id, **payload)
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO source_review_cards VALUES (?, ?, ?, ?, ?)",
                            (operator, message_id, channel_id, token, json.dumps(actions)))

    async def _ask_input(self, bot, operator, channel, field):
        source = bot._get_source(channel)
        if source is None or source["topic_id"] is None:
            return
        title = " ".join(source["title"].split())[:150]
        label = INPUT_LABELS[field]
        command = "/review" if field == "title" else field
        request = {
            "title": "Введите название нового топика",
            "/reactions": "Введите минимальное число реакций (целое число от 0)",
            "/age": "Введите число часов до замера (целое число от 1)",
        }[field]
        result = await bot.api(
            "sendMessage", chat_id=bot.chat_id, message_thread_id=bot.topic_id,
            text=f"{request} для канала «{title}» ответом на это сообщение.\n"
                 f"Чтобы отменить, отправьте {command}.",
            reply_markup={"force_reply": True, "input_field_placeholder": label},
        )
        prompt_id = result.get("message_id") if isinstance(result, dict) else None
        if identifier(prompt_id):
            # An empty token means this card only accepts a reply to its message.
            with self.db:
                self.db.execute("INSERT OR REPLACE INTO source_review_cards VALUES (?, ?, ?, '', ?)",
                                (operator, prompt_id, channel, json.dumps([field])))

    def pending_input(self, operator, message):
        reply = message.get("reply_to_message")
        if not isinstance(reply, dict) or not identifier(reply.get("message_id")):
            return None
        row = self.db.execute("""
            SELECT channel_id, actions FROM source_review_cards WHERE operator_id=? AND message_id=? AND token=''
        """, (operator, reply["message_id"])).fetchone()
        if row:
            field, = json.loads(row["actions"]) or ["title"]
            return row["channel_id"], field

    def _clear_input(self, operator):
        with self.db:
            self.db.execute("DELETE FROM source_review_cards WHERE operator_id=? AND token=''", (operator,))

    async def apply_input(self, bot, operator, update_id, channel, field, text):
        if field == "title":
            await self.apply_title(bot, operator, update_id, channel, text)
            return
        args = ["manual", text.strip()] if field == "/reactions" else [text.strip()]
        await self._apply_control(bot, operator, update_id, channel, field, args)

    async def apply_title(self, bot, operator, update_id, channel, text):
        title = " ".join(text.split())
        reserved = {_normalize(PENDING_CATEGORY), _normalize(EXCLUDED_CATEGORY)}
        if not 1 <= len(title) <= 128 or _visible(title) != title or _normalize(title) in reserved:
            await bot.reply("Ответьте на исходный запрос названием от 1 до 128 символов. Выберите обычное название темы.")
            return
        source = bot._get_source(channel)
        if source is None or source["topic_id"] is None:
            await bot.reply("Маршрут канала изменился. Откройте /review заново.")
            return
        try:
            titles = await self.list_topics()
            title = next((name for _, name in titles if _normalize(name) == _normalize(title)), title)
            topic_id = await self.ensure_topic(title)
        except READ_ERRORS:
            await bot.reply("Не удалось создать или открыть топик. Проверьте название и право управления темами, "
                            "затем ответьте на исходный запрос ещё раз.")
            return
        response = bot._change_source("/retopic", operator, update_id, channel,
                                      {"topic_id": topic_id, "category": title})
        self._clear_input(operator)
        await self._card(bot, operator, response, [[EDIT_BUTTON], [HOME_BUTTON]])

    async def click(self, bot, operator, update_id, query):
        data = query.get("data")
        card = self.db.execute("SELECT * FROM source_review_cards WHERE operator_id=?", (operator,)).fetchone()
        prefix = f"review:{card['token']}:" if card and card["token"] else None
        if (not prefix or card["message_id"] != query["message"].get("message_id")
                or not isinstance(data, str) or not data.startswith(prefix)):
            await bot.api("answerCallbackQuery", callback_query_id=query["id"], text="Карточка устарела. /review")
            return
        index = data[len(prefix):]
        actions = json.loads(card["actions"])
        if not index.isascii() or not index.isdecimal() or len(index) > 3 or int(index) >= len(actions):
            return
        kind, value = actions[int(index)]
        # Previously rendered policy buttons can still be present in Telegram.
        if kind in ("controls", "control", "apply_control") and value[0] == "/policy":
            kind = "settings"
        channel = card["channel_id"]
        message = card["message_id"]
        await bot.api("answerCallbackQuery", callback_query_id=query["id"])
        if kind == "channels":
            await self.show_channels(bot, operator, page=value, message_id=message)
            return
        if kind == "home":
            await self.show(bot, operator, message_id=message)
            return
        if kind == "edit":
            await self.show_edit(bot, operator, value, message_id=message)
            return
        if kind == "edit_page":
            await self.show_edit(bot, operator, channel, page=value, message_id=message)
            return
        if kind == "title":
            await self._ask_input(bot, operator, channel, "title")
            return
        if kind == "settings":
            await bot._show_settings(message_id=message)
            with self.db:
                self.db.execute("DELETE FROM source_review_cards WHERE operator_id=?", (operator,))
            return
        if kind == "controls":
            command, page = value
            await self.show_controls(bot, operator, command, page=page, message_id=message)
            return
        if kind == "control":
            command, target = value
            await self._show_control(bot, operator, command, target, message_id=message)
            return
        if kind == "input":
            await self._ask_input(bot, operator, channel, value)
            return
        if kind == "apply_control":
            command, *args = value
            await self._apply_control(bot, operator, update_id, channel, command, args, message_id=message)
            return
        if kind == "move":
            if bot._processed(update_id):
                return
            topic_id, title = value
            topics = await self._topic_titles(bot)
            if topics is None:
                return
            if topics.get(topic_id) != title or topic_id == bot.topic_id:
                await bot.reply("Тема изменилась. Откройте список заново через /review.")
                return
            response = bot._change_source("/retopic", operator, update_id, channel,
                                          {"topic_id": topic_id, "category": title})
            await self._card(bot, operator, response, [[EDIT_BUTTON], [HOME_BUTTON]], message_id=message)
            return
        if not self.pending(0, channel) or bot._processed(update_id):
            await self.show(bot, operator, after=channel, message_id=message)
            return
        if kind == "page":
            await self.show(bot, operator, channel_id=channel, page=value, message_id=message)
            return
        if kind == "later":
            await self.show(bot, operator, after=channel, message_id=message)
            return
        if kind == "exclude":
            changes = {"category": EXCLUDED_CATEGORY, "enabled": 0}
        else:
            try:
                if not await self.validate_source(channel):
                    await bot.reply("Канал недоступен. Проверьте подписку и повторите выбор.")
                    return
                if kind == "create":
                    category = value
                    topic_id = await self.ensure_topic(category)
                else:
                    topic_id, category = value
                    if (topic_id, category) not in await self.list_topics():
                        await bot.reply("Тема изменилась. Откройте новую карточку командой /review.")
                        return
            except READ_ERRORS:
                LOG.warning("Не удалось применить маршрут из карточки")
                await bot.reply("Не удалось подтвердить маршрут. Проверьте доступ к каналу и право управления темами.")
                return
            changes = {"category": category, "topic_id": topic_id, "enabled": 1,
                       "reaction_hours": 12 if _normalize(category) == _normalize("Юмор") else 2}
        bot._change_source("/review", operator, update_id, channel, changes)
        await self.show(bot, operator, after=channel, message_id=message)
        LOG.info("Решение в разборе сохранено для канала %s", channel)
