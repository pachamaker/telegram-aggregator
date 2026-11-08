import copy
import unittest
from unittest.mock import AsyncMock

from aggregator.admin import AdminBot, BotAPIError
from aggregator.review import Review
from aggregator.store import open_db


class ReviewTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.db = open_db(":memory:")
        self.addCleanup(self.db.close)
        self.db.executescript("""
            INSERT INTO sources(channel_id, title, category, topic_id, enabled)
            VALUES (100, 'Test memes', 'Юмор', NULL, 0),
                   (200, 'Test AI', 'AI', NULL, 0);
        """)
        self.topics = [(7, "Настройки"), (10, "Юмор")]
        self.list_topics = AsyncMock(side_effect=lambda: self.topics)
        self.ensure_topic = AsyncMock(return_value=22)
        self.validate_source = AsyncMock(return_value=True)
        self.review = Review(self.db, ["Юмор", "AI", "Настройки"], self.list_topics,
                             self.ensure_topic, self.validate_source)
        self.bot = AdminBot(self.db, {
            "group_id": 123, "admin_topic_id": 7, "operator_ids": [42, 43],
        }, "fake-token", AsyncMock(), self.review)
        self.bot.api = AsyncMock(side_effect=self.request)
        self.message = {
            "message_id": 70, "chat": {"id": self.bot.chat_id, "type": "supergroup", "is_forum": True},
            "message_thread_id": 7, "is_topic_message": True,
        }
        self.operator = {"id": 42, "is_bot": False}
        self.payload = None
        self.role = "administrator"

    async def request(self, method, **payload):
        if method == "getChatMember":
            return {"status": self.role, "user": {"id": payload["user_id"]}}
        if method in ("sendMessage", "editMessageText"):
            self.payload = payload
            if payload.get("reply_markup", {}).get("force_reply"):
                return {"message_id": 71}
            return {"message_id": 70}
        if method == "answerCallbackQuery":
            return True
        raise AssertionError(method)

    async def show(self, text="/review"):
        message = {**self.message, "from": self.operator, "text": text,
                   "entities": [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]}
        await self.bot.handle_update({"update_id": 1, "message": message})

    def click(self, label, update_id=2, operator=None):
        buttons = [button for row in self.payload["reply_markup"]["inline_keyboard"] for button in row]
        data = next(button["callback_data"] for button in buttons if button["text"] == label)
        return {"update_id": update_id, "callback_query": {
            "id": str(update_id), "from": operator or self.operator,
            "message": copy.deepcopy(self.message), "data": data,
        }}

    def source(self, channel_id=100):
        return self.db.execute("SELECT * FROM sources WHERE channel_id=?", (channel_id,)).fetchone()

    async def open_control(self, command, *, update_id=2):
        with self.db:
            self.db.execute("UPDATE sources SET topic_id=10, enabled=1 WHERE channel_id=100")
        await self.show(command)
        label = "Test memes · активен" if command == "/pause" else "Test memes"
        await self.bot.handle_update(self.click(label, update_id=update_id))

    async def test_pause_and_resume_use_one_screen_and_keep_existing_queue_semantics(self):
        await self.open_control("/pause")
        self.assertIn("Канал активен", self.payload["text"])
        with self.db:
            self.db.execute("INSERT INTO cursors VALUES (100, 25)")
            self.db.execute("""
                INSERT INTO queue(channel_id, post_ids, published_at, due, status, policy)
                VALUES (100, '[20]', 100, 120, 'pending', 'v2')
            """)
        await self.bot.handle_update(self.click("Приостановить пересылку", update_id=3))
        self.assertEqual(0, self.source()["enabled"])
        self.assertEqual("skipped", self.db.execute("SELECT status FROM queue").fetchone()[0])
        await self.bot.handle_update(self.click("Возобновить с новых постов", update_id=4))
        self.assertEqual(1, self.source()["enabled"])
        self.assertIsNone(self.db.execute("SELECT last_id FROM cursors").fetchone())

    async def test_reaction_screen_accepts_only_a_valid_reply_to_the_threshold_prompt(self):
        await self.open_control("/reactions")
        await self.bot.handle_update(self.click("Ручной порог…", update_id=3))
        self.assertTrue(self.payload["reply_markup"]["force_reply"])
        message = {**self.message, "from": self.operator, "reply_to_message": {"message_id": 71}}
        await self.bot.handle_update({"update_id": 4, "message": {**message, "text": "-1"}})
        self.assertEqual("auto", self.source()["reaction_mode"])
        await self.bot.handle_update({"update_id": 5, "message": {**message, "text": "5"}})
        self.assertEqual(("manual", 5), (self.source()["reaction_mode"], self.source()["threshold"]))
        await self.bot.handle_update({"update_id": 6, "message": {**message, "text": "9"}})
        self.assertEqual(5, self.source()["threshold"])
        self.assertIn("Ручной порог · 5", self.payload["text"])

    async def test_age_screen_saves_presets_and_custom_hours(self):
        await self.open_control("/age")
        self.assertIn("после публикации", self.payload["text"])
        await self.bot.handle_update(self.click("6 ч", update_id=3))
        self.assertEqual(6, self.source()["reaction_hours"])
        await self.bot.handle_update(self.click("Другое число…", update_id=4))
        message = {**self.message, "from": self.operator, "text": "8",
                   "reply_to_message": {"message_id": 71}}
        await self.bot.handle_update({"update_id": 5, "message": message})
        self.assertEqual(8, self.source()["reaction_hours"])

    async def test_old_policy_button_returns_to_settings_without_changing_policy(self):
        await self.review._card(self.bot, 42, "Старая карточка",
                                [[("Правила v1", "apply_control", ["/policy", "legacy"])]],
                                channel_id=100)
        await self.bot.handle_update(self.click("Правила v1"))
        self.assertEqual("v2", self.source()["policy"])
        self.assertIn("Настройки подборки", self.payload["text"])
        self.assertIsNone(self.db.execute("SELECT 1 FROM source_review_cards").fetchone())

    async def open_retopic(self):
        with self.db:
            self.db.execute("UPDATE sources SET topic_id=10, reaction_hours=12 WHERE channel_id=100")
            self.db.execute("UPDATE sources SET topic_id=10, enabled=1 WHERE channel_id=200")
            self.db.execute("INSERT INTO cursors VALUES (100, 25)")
            self.db.execute("""
                INSERT INTO queue(channel_id, post_ids, published_at, due, status, policy, topic_id)
                VALUES (100, '[20]', 100, 120, 'sending', 'v2', 10)
            """)
        self.topics.append((20, "Разработка"))
        await self.show()
        await self.bot.handle_update(self.click("Изменить топик каналу"))
        await self.bot.handle_update(self.click("Test memes · Юмор", update_id=3))

    async def test_retopic_changes_category_and_destination_without_touching_runtime_state(self):
        await self.open_retopic()
        self.assertIn("Юмор", self.payload["text"])
        self.assertNotIn("Настройки", str(self.payload["reply_markup"]))
        await self.bot.handle_update(self.click("Разработка", update_id=4))
        source = self.source()
        self.assertEqual((20, "Разработка", 0, 12), tuple(
            source[field] for field in ("topic_id", "category", "enabled", "reaction_hours")
        ))
        self.assertEqual(25, self.db.execute("SELECT last_id FROM cursors").fetchone()[0])
        self.assertEqual(("sending", 10), tuple(self.db.execute("SELECT status, topic_id FROM queue").fetchone()))
        self.assertEqual(10, self.source(200)["topic_id"])
        self.ensure_topic.assert_not_awaited()

    async def test_custom_topic_requires_a_reply_from_the_operator_and_is_applied_once(self):
        await self.open_retopic()
        await self.bot.handle_update(self.click("Создать топик…", update_id=4))
        self.assertTrue(self.payload["reply_markup"]["force_reply"])
        message = {**self.message, "from": self.operator, "text": "Новый топик",
                   "reply_to_message": {"message_id": 71}}
        for wrong in (
            {"reply_to_message": {"message_id": 70}},
            {"from": {"id": 43, "is_bot": False}},
            {"message_thread_id": 8},
        ):
            await self.bot.handle_update({"update_id": 5, "message": {**message, **wrong}})
        self.ensure_topic.assert_not_awaited()
        self.bot.review = Review(self.db, ["AI"], self.list_topics, self.ensure_topic, self.validate_source)
        await self.bot.handle_update({"update_id": 6, "message": message})
        await self.bot.handle_update({"update_id": 6, "message": message})
        await self.bot.handle_update({"update_id": 7, "message": {**message, "text": "Старый запрос"}})
        self.ensure_topic.assert_awaited_once_with("Новый топик")
        self.assertEqual((22, "Новый топик"), (self.source()["topic_id"], self.source()["category"]))

    async def test_retopic_shortcut_paginates_channels_and_uses_current_topic_titles(self):
        with self.db:
            self.db.executemany("INSERT INTO sources(channel_id,title,category,topic_id) VALUES (?,?,?,?)",
                                [(300 + i, f"Channel {i:02}", "Старая категория", 10) for i in range(10)])
        await self.show("/retopic")
        self.assertIn("Channel 00 · Юмор", str(self.payload["reply_markup"]))
        await self.bot.handle_update(self.click("Ещё каналы →"))
        self.assertIn("Channel 09 · Юмор", str(self.payload["reply_markup"]))
        self.assertNotIn("Channel 00", str(self.payload["reply_markup"]))

    async def test_existing_topic_applies_route_and_advances_in_the_same_card(self):
        await self.show()
        self.assertIn("Test memes", self.payload["text"])
        self.assertNotIn("Настройки", str(self.payload["reply_markup"]))
        await self.bot.handle_update(self.click("Юмор"))
        self.assertEqual((10, "Юмор", 1, 12), tuple(
            self.source()[field] for field in ("topic_id", "category", "enabled", "reaction_hours")
        ))
        self.assertIn("Test AI", self.payload["text"])
        self.assertEqual(70, self.payload["message_id"])
        self.assertEqual(1, self.db.execute("SELECT count(*) FROM admin_changes").fetchone()[0])
        self.ensure_topic.assert_not_awaited()

    async def test_new_category_creates_topic_only_after_click(self):
        await self.show("/review 200")
        self.ensure_topic.assert_not_awaited()
        await self.bot.handle_update(self.click("+ AI"))
        self.ensure_topic.assert_awaited_once_with("AI")
        self.assertEqual(22, self.source(200)["topic_id"])
        self.assertEqual(2, self.source(200)["reaction_hours"])
        self.assertIn("больше нет каналов", self.payload["text"])
        self.assertIsNone(self.source()["topic_id"])

    async def test_stale_card_and_duplicate_update_cannot_route_next_channel(self):
        await self.show()
        old_click = self.click("Юмор")
        await self.bot.handle_update(old_click)
        await self.bot.handle_update(old_click)
        self.assertIsNone(self.source(200)["topic_id"])
        self.assertEqual(1, self.db.execute("SELECT count(*) FROM admin_changes").fetchone()[0])

    async def test_other_operator_wrong_topic_and_revoked_admin_cannot_click(self):
        await self.show()
        click = self.click("Юмор", operator={"id": 43, "is_bot": False})
        await self.bot.handle_update(click)
        click = self.click("Юмор")
        click["callback_query"]["message"]["message_thread_id"] = 8
        await self.bot.handle_update(click)
        self.role = "member"
        await self.bot.handle_update(self.click("Юмор"))
        self.assertIsNone(self.source()["topic_id"])

    async def test_changed_topic_or_unavailable_source_remains_pending(self):
        await self.show()
        click = self.click("Юмор")
        self.topics = [(7, "Настройки"), (10, "Changed")]
        await self.bot.handle_update(click)
        self.assertIsNone(self.source()["topic_id"])
        await self.show()
        self.validate_source.return_value = False
        await self.bot.handle_update(self.click("Changed"))
        self.assertIsNone(self.source()["topic_id"])

    async def test_later_keeps_pending_and_exclude_persists(self):
        await self.show()
        await self.bot.handle_update(self.click("Позже"))
        self.assertIsNone(self.source()["topic_id"])
        self.assertIn("Test AI", self.payload["text"])
        await self.bot.handle_update(self.click("Исключить", update_id=3))
        self.assertEqual("Исключён", self.source(200)["category"])
        self.assertEqual(0, self.source(200)["enabled"])
        await self.show()
        self.assertIn("Test memes", self.payload["text"])

    async def test_topic_creation_failure_preserves_pending_source(self):
        await self.show("/review 200")
        self.ensure_topic.side_effect = ValueError("No manage_topics")
        await self.bot.handle_update(self.click("+ AI"))
        self.assertIsNone(self.source(200)["topic_id"])
        self.assertIn("право управления темами", self.payload["text"])

    async def test_pagination_and_restart_keep_card_working(self):
        self.topics += [(100 + i, f"Topic {i}") for i in range(12)]
        await self.show()
        await self.bot.handle_update(self.click("Ещё темы →"))
        self.assertIn("← Темы", str(self.payload["reply_markup"]))
        click = self.click("← Темы", update_id=3)
        self.bot.review = Review(self.db, ["AI"], self.list_topics, self.ensure_topic, self.validate_source)
        await self.bot.handle_update(click)
        self.assertIn("Юмор", str(self.payload["reply_markup"]))

    async def test_expired_callback_does_not_block_other_updates(self):
        await self.show()
        click = self.click("Юмор")
        original = self.bot.api.side_effect

        async def expired(method, **payload):
            if method == "answerCallbackQuery":
                raise BotAPIError(400)
            return await original(method, **payload)

        self.bot.api.side_effect = expired
        await self.bot.handle_update(click)
        self.assertIsNone(self.source()["topic_id"])
        self.assertIn("/review", self.payload["text"])


if __name__ == "__main__":
    unittest.main()
