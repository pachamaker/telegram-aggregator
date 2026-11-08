import json
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from telethon import errors, functions, types

from aggregator.runtime import Pass, enqueue, recompute_baseline
from aggregator.store import migrate_v1, open_db, seed_sources, validate_config

LEGACY = {"hours": 12, "threshold": 7, "news_topics": ["Новости"], "dedup_hours": 72}
GROUP = types.InputPeerChannel(900, 0)


def post(channel, message_id, text, when, reactions=0):
    return types.Message(
        id=message_id, peer_id=types.PeerChannel(channel), message=text,
        date=datetime.fromtimestamp(when, timezone.utc),
        reactions=types.MessageReactions([types.ReactionCount(types.ReactionEmoji("👍"), reactions)]),
    )


class Telegram:
    def __init__(self, posts, *, reactions_enabled=True, fail_verification=False):
        self.posts = {(p.peer_id.channel_id, p.id): p for p in posts}
        self.sent = {}
        self.reactions_enabled = reactions_enabled
        self.fail_verification = fail_verification

    async def __call__(self, request):
        if isinstance(request, functions.channels.GetFullChannelRequest):
            return SimpleNamespace(full_chat=SimpleNamespace(
                available_reactions=(types.ChatReactionsAll() if self.reactions_enabled
                                     else types.ChatReactionsNone()),
                participants_count=1000,
            ))
        if isinstance(request, functions.messages.GetForumTopicsByIDRequest):
            topic = object.__new__(types.ForumTopic)
            topic.id = request.topics[0]
            topic.peer = types.PeerChannel(GROUP.channel_id)
            topic.closed = topic.hidden = False
            return SimpleNamespace(topics=[topic])
        if isinstance(request, functions.messages.ForwardMessagesRequest):
            updates = []
            for random_id in request.random_id:
                message_id = 100 + len(self.sent)
                self.sent[message_id] = SimpleNamespace(
                    id=message_id, reply_to=SimpleNamespace(reply_to_top_id=request.top_msg_id))
                updates.append(types.UpdateMessageID(message_id, random_id))
            return SimpleNamespace(updates=updates)
        raise AssertionError(type(request))

    async def get_messages(self, peer, ids):
        if peer == GROUP:
            if self.fail_verification:
                raise TimeoutError("simulated lost read-back")
            return [self.sent.get(i) for i in ids]
        return [self.posts.get((peer.channel_id, i)) for i in ids]

    async def download_media(self, post, file, *, thumb=None):
        file.write(b"abcde")
        return file


class RuntimeTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.db = open_db(":memory:")
        self.addCleanup(self.db.close)
        self.peers = {100: types.InputPeerChannel(100, 0), 200: types.InputPeerChannel(200, 0)}
        self.now = int(time.time())

    def put(self, channel, posts, *, mode="auto", threshold=None, policy="v2"):
        with self.db:
            self.db.execute("""
                INSERT INTO sources(channel_id,title,category,topic_id,reaction_mode,threshold,policy)
                VALUES (?,?,'Новости',?,?,?,?)
            """, (channel, str(channel), channel + 1, mode, threshold, policy))
        source = self.db.execute("SELECT * FROM sources WHERE channel_id=?", (channel,)).fetchone()
        enqueue(self.db, source, posts, posts[-1].id, LEGACY)

    async def execute_pass(self, telegram, *, max_media_bytes=0):
        await Pass(telegram, self.db, GROUP, self.peers, {"max_media_bytes": max_media_bytes}, LEGACY).run()

    def outcomes(self):
        return {(row["channel_id"], row["post_ids"], row["lane"]): row["status"]
                for row in self.db.execute("SELECT * FROM queue")}

    async def test_ads_and_exact_copies_are_excluded_across_topics(self):
        a = [post(100, 1, "Отдельный пересказ о событии", self.now - 5),
             post(100, 2, "Реклама. erid: 2VtzqwXJSQ", self.now - 4)]
        b = [post(200, 1, "Отдельный пересказ о событии", self.now - 3),
             post(200, 2, "Новая публикация о том же событии", self.now - 2)]
        self.put(100, a, mode="off")
        self.put(200, b, mode="off")
        telegram = Telegram(a + b)
        await self.execute_pass(telegram)
        self.assertEqual(len(telegram.sent), 2)
        self.assertEqual(self.outcomes(), {(100, "[1]", "live"): "sent", (100, "[2]", "live"): "skipped",
                                          (200, "[1]", "live"): "skipped", (200, "[2]", "live"): "sent"})
        reasons = [r[0] for r in self.db.execute("SELECT reason FROM decisions ORDER BY id")]
        self.assertEqual(reasons, ["published", "ad_disclosure", "duplicate", "published"])

    async def test_manual_threshold_waits_only_when_reactions_available(self):
        a = [post(100, 1, "Свежий пост", self.now - 60),
             post(100, 2, "Созревший пост", self.now - 7300)]
        self.put(100, a, mode="manual", threshold=2)
        telegram = Telegram(a)
        await self.execute_pass(telegram)
        self.assertEqual(self.outcomes(), {(100, "[1]", "live"): "pending", (100, "[2]", "live"): "skipped"})
        row = self.db.execute("SELECT due,published_at FROM queue WHERE post_ids='[1]'").fetchone()
        self.assertEqual(row["due"] - row["published_at"], 7200)

        b = [post(200, 1, "Канал без реакций", self.now - 60)]
        self.put(200, b, mode="manual", threshold=2)
        telegram = Telegram(a + b, reactions_enabled=False)
        await self.execute_pass(telegram)
        self.assertEqual(self.outcomes()[(200, "[1]", "live")], "sent")

    async def test_shadow_rules_never_send_or_change_legacy_delivery(self):
        a = [post(100, 1, "Реклама. erid: 2VtzqwXJSQ", self.now - 5),
             post(100, 2, "Самостоятельный пост", self.now - 4)]
        self.put(100, a, mode="off", policy="shadow")
        telegram = Telegram(a)
        await self.execute_pass(telegram)
        self.assertEqual(len(telegram.sent), 2)
        self.assertEqual(self.outcomes(), {(100, "[1]", "live"): "sent", (100, "[1]", "shadow"): "skipped",
                                          (100, "[2]", "live"): "sent", (100, "[2]", "shadow"): "sent"})
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM seen WHERE lane='shadow'").fetchone()[0], 2)

    async def test_transition_does_not_republish_exact_v2_copy_from_legacy_source(self):
        a = [post(100, 1, "Полная копия при переходе", self.now - 5)]
        b = [post(200, 1, "Полная копия при переходе", self.now - 4)]
        self.put(100, a, mode="off")
        self.put(200, b, mode="off", policy="legacy")
        telegram = Telegram(a + b)
        await self.execute_pass(telegram)
        self.assertEqual(len(telegram.sent), 1)
        self.assertEqual(self.outcomes()[(200, "[1]", "live")], "skipped")

    async def test_legacy_media_is_fingerprinted_for_a_later_v2_copy(self):
        a = post(100, 1, "", self.now - 5)
        b = post(200, 1, "", self.now - 4)
        for item, media_id in ((a, 100), (b, 200)):
            item.media = types.MessageMediaDocument(document=types.Document(
                media_id, 0, b"", item.date, "application/octet-stream", 5, 1, []
            ))
        self.put(100, [a], mode="off", policy="legacy")
        self.put(200, [b], mode="off")
        telegram = Telegram([a, b])
        await self.execute_pass(telegram, max_media_bytes=5)
        self.assertEqual(len(telegram.sent), 1)
        self.assertEqual(self.outcomes()[(200, "[1]", "live")], "skipped")

    async def test_uncertain_send_blocks_an_identical_copy_and_is_not_retried(self):
        a = [post(100, 1, "Та же полная публикация", self.now - 5)]
        b = [post(200, 1, "Та же полная публикация", self.now - 4)]
        self.put(100, a, mode="off")
        self.put(200, b, mode="off")
        telegram = Telegram(a + b, fail_verification=True)
        await self.execute_pass(telegram)
        await self.execute_pass(telegram)
        self.assertEqual(len(telegram.sent), 1)
        self.assertEqual(self.outcomes(), {(100, "[1]", "live"): "sending", (200, "[1]", "live"): "pending"})

    async def test_unresolved_import_without_keys_blocks_live_sending(self):
        a = [post(100, 1, "Пост после переноса", self.now - 5)]
        self.put(100, a, mode="off")
        with self.db:
            self.db.execute("""
                INSERT INTO queue(channel_id, post_ids, published_at, due, status, policy)
                VALUES (100,'[15]',0,0,'sending','legacy')
            """)
        telegram = Telegram(a)
        await self.execute_pass(telegram)
        self.assertFalse(telegram.sent)
        self.assertEqual(self.outcomes()[(100, "[1]", "live")], "pending")

    async def test_rate_limit_after_pause_does_not_reactivate_inflight_job(self):
        a = [post(100, 1, "Пост до паузы", self.now - 5)]
        self.put(100, a, mode="off")
        db = self.db

        class Paused(Telegram):
            async def __call__(self, request):
                if isinstance(request, functions.messages.ForwardMessagesRequest):
                    with db:
                        db.execute("UPDATE queue SET cancel_retry=1 WHERE status='sending'")
                    raise errors.FloodWaitError(request, capture=10)
                return await super().__call__(request)

        with self.assertRaises(errors.FloodWaitError):
            await self.execute_pass(Paused(a))
        self.assertEqual(self.outcomes()[(100, "[1]", "live")], "skipped")

    async def test_slow_mode_is_a_known_rejection_and_schedules_retry(self):
        a = [post(100, 1, "Ожидающий пост", self.now - 5)]
        self.put(100, a, mode="off")

        class Slow(Telegram):
            async def __call__(self, request):
                if isinstance(request, functions.messages.ForwardMessagesRequest):
                    raise errors.SlowModeWaitError(request, capture=60)
                return await super().__call__(request)

        with self.assertRaises(errors.SlowModeWaitError):
            await self.execute_pass(Slow(a))
        self.assertEqual(self.outcomes()[(100, "[1]", "live")], "pending")
        self.assertGreaterEqual(self.db.execute("SELECT retry_at FROM queue").fetchone()[0], self.now + 60)

    async def test_cold_start_collects_later_even_if_already_delivered(self):
        a = [post(100, 1, "Пост для калибровки", self.now - 7300, reactions=5)]
        self.put(100, a)
        await self.execute_pass(Telegram(a))
        self.assertEqual(self.outcomes()[(100, "[1]", "live")], "sent")
        self.assertEqual(self.db.execute("SELECT count FROM measurements").fetchone()[0], 5)

    def test_baseline_needs_twenty_same_age_samples(self):
        a = [post(100, 1, "Первый пост", self.now - 30)]
        self.put(100, a)
        source = self.db.execute("SELECT * FROM sources").fetchone()
        with self.db:
            self.db.executemany("INSERT INTO measurements VALUES (100,?,?,2,?,?)",
                                [(json.dumps([i]), self.now - 50, i, self.now - 50) for i in range(2, 21)])
        self.assertIsNone(recompute_baseline(self.db, source, self.now))
        with self.db:
            self.db.execute("INSERT INTO measurements VALUES (100,'[21]',?,2,21,?)", (self.now, self.now))
        self.assertEqual(recompute_baseline(self.db, source, self.now), 12)


class MigrationTest(unittest.TestCase):
    def test_migrate_keeps_cursor_due_and_uncertain_send(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "v1.sqlite3"
            old = sqlite3.connect(path)
            old.executescript("""
                CREATE TABLE cursors(channel TEXT, last_id INTEGER);
                CREATE TABLE pending(channel TEXT, post_ids TEXT, due INTEGER, status TEXT);
                CREATE TABLE seen(key TEXT, published_at INTEGER);
                INSERT INTO cursors VALUES ('@example', 14);
                INSERT INTO pending VALUES ('@example','[13]',11000,'sending');
                INSERT INTO pending VALUES ('@example','[14]',12000,'pending');
                INSERT INTO seen VALUES ('memes:origin:100:12',10000);
            """)
            old.close()
            before = path.read_bytes()
            config = {
                "destination": {"group_id": 900, "topic_ids": {"memes": 101}},
                "sources": [{"id": 100, "channel": "@example", "topic": "memes"}],
                "memes": {"check_after_hours": 12},
                "news_deduplication": {"topics": [], "lookback_hours": 72},
            }
            db = open_db(":memory:")
            self.addCleanup(db.close)
            self.assertEqual(migrate_v1(db, path, config, preview=True)["unresolved"], 1)
            self.assertFalse(db.execute("SELECT 1 FROM queue").fetchall())
            migrate_v1(db, path, config)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(tuple(db.execute("SELECT * FROM cursors").fetchone()), (100, 14))
            self.assertEqual([tuple(r) for r in db.execute(
                "SELECT due,status,policy,topic_id FROM queue ORDER BY id")],
                [(11000, "sending", "legacy", 101), (12000, "pending", "legacy", None)])
            seed_sources(db, validate_config({"group_id": 900, "folder_id": 2}), 900)
            self.assertEqual(db.execute("SELECT policy FROM sources").fetchone()[0], "legacy")
            self.assertEqual(db.execute("SELECT value FROM settings WHERE key='folder_id'").fetchone()[0], "2")
            with self.assertRaises(ValueError):
                migrate_v1(db, path, config)
