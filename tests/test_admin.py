import asyncio
import json
import sqlite3
import unittest
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.error import HTTPError, URLError

from aggregator.admin import AdminBot, BotAPIError, _bot_request, run_admin
from aggregator.store import open_db


CONFIG = {"group_id": 123, "admin_topic_id": 7, "operator_ids": [42]}
CHAT = -1000000000123


def update(text, update_id=100, **message):
    command = text.split()[0]
    return {
        "update_id": update_id,
        "message": {
            "message_id": 20,
            "date": 1700000000,
            "chat": {"id": CHAT, "type": "supergroup", "is_forum": True},
            "message_thread_id": 7,
            "is_topic_message": True,
            "from": {"id": 42, "is_bot": False, "first_name": "Operator"},
            "text": text,
            "entities": [{"type": "bot_command", "offset": 0, "length": len(command)}],
            **message,
        },
    }


class AdminTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.db = open_db(":memory:")
        self.addCleanup(self.db.close)
        self.db.executescript("""
            INSERT INTO sources (channel_id, title, category, topic_id)
                VALUES (900, 'Test Source', 'news', 10);
            INSERT INTO cursors VALUES (900, 25);
            INSERT INTO queue (channel_id, post_ids, published_at, due, status, policy, topic_id, lane)
                VALUES (900, '[20]', 1700000000, 1700000100, 'pending', 'v2', 10, 'live'),
                       (900, '[21]', 1700000000, 1700000100, 'pending', 'shadow', 10, 'shadow'),
                       (900, '[22]', 1700000000, 1700000100, 'sending', 'v2', 10, 'live');
        """)
        self.validate_topic = AsyncMock(return_value=True)
        self.request_patch = patch("aggregator.admin._bot_request", side_effect=self.request)
        self.request_patch.start()
        self.addCleanup(self.request_patch.stop)
        self.bot = AdminBot(self.db, CONFIG, "fake-token", self.validate_topic)
        self.responses = []
        self.message_calls = []
        self.status = "creator"

    def request(self, _token, method, data):
        if method == "getChatMember":
            self.assertEqual(data, {"chat_id": CHAT, "user_id": 42})
            return {"status": self.status, "user": {"id": 42, "is_bot": False, "first_name": "Operator"}}
        if method == "getMe":
            return {"id": 1001, "is_bot": True, "first_name": "Bot", "username": "digest_bot"}
        if method in ("sendMessage", "editMessageText"):
            self.assertEqual(data["chat_id"], CHAT)
            if method == "sendMessage":
                self.assertEqual(data["message_thread_id"], 7)
            else:
                self.assertEqual(data["message_id"], 70)
            self.message_calls.append((method, data))
            self.responses.append(data["text"])
            return {"message_id": 70}
        if method == "answerCallbackQuery":
            return True
        raise AssertionError(method)

    def source(self):
        return self.db.execute("SELECT * FROM sources").fetchone()

    async def test_sender_must_be_identifiable_and_in_the_configured_forum_topic(self):
        for fields in (
            {"chat": {"id": CHAT - 1, "type": "supergroup", "is_forum": True}},
            {"chat": {"id": CHAT, "type": "supergroup", "is_forum": False}},
            {"message_thread_id": 8},
            {"from": {"id": "42", "is_bot": False}},
            {"from": {"id": 43, "is_bot": False}},
            {"sender_chat": {"id": CHAT}},
            {"forward_origin": {"type": "user"}},
            {"is_automatic_forward": True},
            {"from": {"id": 42, "is_bot": True}},
            {"entities": [{"type": "code", "offset": 0, "length": 6}]},
        ):
            with self.subTest(fields=fields):
                await self.bot.handle_update(update("/pause 900", **fields))
        self.assertEqual(self.source()["enabled"], 1)
        self.assertEqual(self.responses, [])

    async def test_member_cannot_change_settings_and_other_bot_suffix_is_ignored(self):
        self.status = "member"
        await self.bot.handle_update(update("/pause 900"))
        self.assertEqual(self.source()["enabled"], 1)
        self.status = "administrator"
        await self.bot.handle_update(update("/pause@other_bot 900", 101))
        self.assertEqual(self.source()["enabled"], 1)
        await self.bot.handle_update(update("/pause@DIGEST_BOT 900", 102))
        self.assertEqual(self.source()["enabled"], 0)

    async def test_duplicate_update_after_restart_keeps_newer_route(self):
        await self.bot.handle_update(update("/route 900 11"))
        await self.bot.handle_update(update("/route 900 12", 101))
        restarted = AdminBot(self.db, CONFIG, "fake-token", self.validate_topic)
        await restarted.handle_update(update("/route 900 11"))
        self.assertEqual(self.source()["topic_id"], 12)
        changes = self.db.execute(
            "SELECT update_id, operator_id, previous, current FROM admin_changes ORDER BY update_id"
        ).fetchall()
        self.assertEqual(
            [(row["update_id"], row["operator_id"], json.loads(row["previous"]), json.loads(row["current"]))
             for row in changes],
            [(100, 42, {"topic_id": 10}, {"topic_id": 11}),
             (101, 42, {"topic_id": 11}, {"topic_id": 12})],
        )

    async def test_failed_audit_insert_rolls_back_the_mutation(self):
        self.db.executescript("""
            CREATE TRIGGER audit_failure BEFORE INSERT ON admin_changes
            BEGIN SELECT RAISE(ABORT, 'cannot write audit'); END;
        """)
        with self.assertRaises(sqlite3.IntegrityError):
            await self.bot.handle_update(update("/route 900 11"))
        self.assertEqual(self.source()["topic_id"], 10)
        self.db.execute("DROP TRIGGER audit_failure")
        await self.bot.handle_update(update("/route 900 11"))
        self.assertEqual(self.source()["topic_id"], 11)

    async def test_route_validates_the_destination_before_write(self):
        self.validate_topic.return_value = False
        await self.bot.handle_update(update("/route 900 11"))
        self.assertEqual(self.source()["topic_id"], 10)
        self.validate_topic.assert_awaited_once_with(11)
        self.assertIn("11", self.responses[-1])

    async def test_route_validation_failure_stays_closed_and_admin_remains_available(self):
        self.validate_topic.side_effect = OSError("Temporary MTProto failure")
        with self.assertLogs("aggregator.admin", level="WARNING"):
            await self.bot.handle_update(update("/route 900 11"))
        self.assertEqual(self.source()["topic_id"], 10)
        self.assertIn("Повторите", self.responses[-1])
        await self.bot.handle_update(update("/source 900", 101))
        self.assertIn("тема 10", self.responses[-1])

    async def test_pause_skips_pending_in_both_lanes_but_preserves_sending(self):
        await self.bot.handle_update(update("/pause 900"))
        self.assertEqual(self.source()["enabled"], 0)
        statuses = self.db.execute("SELECT status FROM queue ORDER BY id").fetchall()
        self.assertEqual([row["status"] for row in statuses], ["skipped", "skipped", "sending"])
        skips = self.db.execute("SELECT post_ids, shadow FROM decisions ORDER BY id").fetchall()
        self.assertEqual([tuple(row) for row in skips], [("[20]", 0), ("[21]", 1)])
        await self.bot.handle_update(update("/skips 900", 101))
        self.assertIn("paused_by_operator", self.responses[-1])

    async def test_resume_requires_explicit_confirmation_and_rebases_cursor_once(self):
        await self.bot.handle_update(update("/pause 900"))
        await self.bot.handle_update(update("/resume 900", 101))
        self.assertEqual(self.source()["enabled"], 0)
        self.assertIn("/resume 900 confirm", self.responses[-1])
        await self.bot.handle_update(update("/resume 900 confirm", 102))
        self.assertEqual(self.source()["enabled"], 1)
        self.assertIsNone(self.db.execute("SELECT last_id FROM cursors").fetchone())
        with self.db:
            self.db.execute("INSERT INTO cursors VALUES (900, 50)")
        await self.bot.handle_update(update("/resume 900 confirm", 103))
        self.assertEqual(self.db.execute("SELECT last_id FROM cursors").fetchone()[0], 50)

    async def test_reactions_validate_manual_threshold_and_show_source_settings(self):
        await self.bot.handle_update(update("/reactions 900 manual -1"))
        self.assertEqual(self.source()["reaction_mode"], "auto")
        await self.bot.handle_update(update("/reactions 900 manual 0", 101))
        self.assertEqual((self.source()["reaction_mode"], self.source()["threshold"]), ("manual", 0))
        await self.bot.handle_update(update("/policy 900 shadow", 102))
        self.assertEqual(self.source()["policy"], "v2")
        await self.bot.handle_update(update("/source 900", 103))
        for value in ("Test Source", "manual", "0", "v2"):
            self.assertIn(value, self.responses[-1])

    async def test_settings_shows_command_help_instead_of_source_details(self):
        await self.bot.handle_update(update("/settings@digest_bot"))
        self.assertEqual(1, len(self.responses))
        self.assertNotIn("Test Source", self.responses[0])
        self.assertNotIn("Последние пропуски\n", self.responses[0])
        self.assertIn("/sources [СТРАНИЦА] — Список каналов и их ID", self.responses[0])
        self.assertIn("/retopic — Сменить топик кнопками", self.responses[0])
        self.assertIn("/review — Разобрать новые каналы из папки", self.responses[0])
        self.assertNotIn("/source ID", self.responses[0])
        self.assertNotIn("/resume", self.responses[0])
        self.assertNotIn("/policy", self.responses[0])

    async def test_sources_links_channels_and_uses_forum_titles_one_page_at_a_time(self):
        self.bot.review = SimpleNamespace(list_topics=AsyncMock(return_value=[(10, "Новости & события")]))
        with self.db:
            self.db.execute("UPDATE sources SET title='Test <Source>', username='test_source' WHERE channel_id=900")
            self.db.executemany(
                "INSERT INTO sources(channel_id, title, category, topic_id) VALUES (?, ?, 'news', 10)",
                [(1000 + i, f"Z Channel {i:02}",) for i in range(1, 14)],
            )
            self.db.execute("INSERT INTO cursors VALUES (1001, 42)")
        await self.bot.handle_update(update("/sources"))
        self.assertIn('<a href="https://t.me/test_source">Test &lt;Source&gt;</a>', self.responses[-1])
        self.assertIn('<a href="https://t.me/c/1001/42">Z Channel 01</a>', self.responses[-1])
        self.assertIn("Новости &amp; события", self.responses[-1])
        self.assertNotIn("ID канала <code>", self.responses[-1])
        self.assertEqual("HTML", self.message_calls[-1][1]["parse_mode"])
        self.assertNotIn("Z Channel 13", self.responses[-1])
        await self.bot.handle_update(update("/sources 2", 101))
        self.assertIn("Z Channel 13 · Новости &amp; события", self.responses[-1])
        self.assertNotIn("Test &lt;Source&gt;", self.responses[-1])
        await self.bot.handle_update(update("/sources 3", 102))
        self.assertIn("Страницы 3 нет", self.responses[-1])

    async def test_sources_buttons_edit_the_same_message_and_return_to_settings(self):
        with self.db:
            self.db.executemany(
                "INSERT INTO sources(channel_id, title, category) VALUES (?, ?, 'news')",
                [(1000 + i, f"Z Channel {i:02}") for i in range(1, 14)],
            )
        await self.bot.handle_update(update("/settings"))

        async def click(label, update_id, **fields):
            rows = self.message_calls[-1][1]["reply_markup"]["inline_keyboard"]
            buttons = [button for row in rows for button in row]
            data = next(button["callback_data"] for button in buttons if button["text"] == label)
            message = update("/settings", message_id=70, **fields)["message"]
            await self.bot.handle_update({"update_id": update_id, "callback_query": {
                "id": str(update_id), "data": data, "from": message["from"], "message": message,
            }})

        await click("Источники", 101, message_thread_id=8)
        self.assertEqual(1, len(self.message_calls))
        self.status = "member"
        await click("Источники", 102)
        self.assertEqual(1, len(self.message_calls))
        self.status = "creator"
        await click("Источники", 103)
        self.assertIn("страница 1 из 2", self.responses[-1])
        await click("Показать ID", 104)
        self.assertIn("ID канала <code>900</code> · ID топика <code>10</code>", self.responses[-1])
        await click("Дальше", 105)
        self.assertIn("страница 2 из 2", self.responses[-1])
        self.assertIn("ID канала <code>1013</code>", self.responses[-1])
        self.assertNotIn("Дальше", str(self.message_calls[-1][1]["reply_markup"]))
        await click("Скрыть ID", 106)
        self.assertNotIn("ID канала <code>", self.responses[-1])
        await click("Назад", 107)
        self.assertIn("страница 1 из 2", self.responses[-1])
        await click("Назад", 108)
        self.assertIn("Настройки подборки", self.responses[-1])
        self.assertEqual(["sendMessage", *(["editMessageText"] * 6)],
                         [method for method, _ in self.message_calls])

    async def test_age_updates_new_decisions_and_displays_matching_baseline(self):
        self.db.executescript("""
            INSERT INTO baselines VALUES (900, 2, 50, 25, 1700000000);
            UPDATE queue SET due=4100000000, retry_at=4200000000 WHERE id=1;
        """)
        await self.bot.handle_update(update("/source 900"))
        self.assertIn("порог 50", self.responses[-1])
        await self.bot.handle_update(update("/age 900 6", 101))
        self.assertEqual(self.source()["reaction_hours"], 6)
        self.assertIn("калибровка", self.responses[-1])
        queue = self.db.execute("SELECT due,retry_at FROM queue WHERE id=1").fetchone()
        self.assertLess(queue["due"], 4100000000)
        self.assertEqual(queue["retry_at"], 4200000000)
        self.assertEqual(self.db.execute("SELECT hours FROM measurements").fetchone()[0], 6)

    def test_http_transport_exception_is_sanitized(self):
        transport = URLError(TimeoutError("https://api.telegram.org/botfake-token/"))
        with patch("aggregator.admin.urlopen", side_effect=transport):
            with self.assertRaises(BotAPIError) as error:
                _bot_request("fake-token", "getUpdates", {"timeout": 1})
        self.assertEqual("getUpdates: TimeoutError", str(error.exception))
        self.assertTrue(error.exception.__suppress_context__)

    def test_http_status_is_distinguished_from_a_transport_failure(self):
        response = HTTPError("https://api.telegram.org/botfake-token/", 503, "fake-token", None,
                             BytesIO(b"Unavailable"))
        with patch("aggregator.admin.urlopen", side_effect=response):
            with self.assertRaises(BotAPIError) as error:
                _bot_request("fake-token", "getMe", {})
        self.assertEqual("getMe: HTTP 503", str(error.exception))

    async def test_poll_retries_then_confirms_processed_update_and_propagates_cancellation(self):
        offsets = []
        loop = asyncio.get_running_loop()
        waiting = asyncio.Event()

        def request(token, method, data):
            if method != "getUpdates":
                return self.request(token, method, data)
            offsets.append(data.get("offset"))
            if len(offsets) == 1:
                with patch("aggregator.admin.urlopen", side_effect=URLError(TimeoutError("fake-token"))):
                    return _bot_request(token, method, data)
            if len(offsets) == 2:
                return [update("/pause 900")]
            loop.call_soon_threadsafe(waiting.set)
            return []

        with patch("aggregator.admin._bot_request", side_effect=request), patch(
            "aggregator.admin.RETRY_DELAY", 0
        ):
            task = asyncio.create_task(run_admin(self.db, CONFIG, "fake-token", self.validate_topic))
            try:
                with self.assertLogs("aggregator.admin", level="WARNING") as logs:
                    await asyncio.wait_for(waiting.wait(), timeout=2)
                self.assertIn("getUpdates: TimeoutError", "\n".join(logs.output))
                self.assertNotIn("fake-token", "\n".join(logs.output))
            finally:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
        self.assertEqual(self.source()["enabled"], 0)
        self.assertEqual(offsets[:3], [None, None, 101])


if __name__ == "__main__":
    unittest.main()
