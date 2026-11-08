import contextlib
import io
import json
import sqlite3
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telethon import errors, functions, types, utils

from aggregator import setup


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
INVITE = "https://t.me/addlist/synthetic-only"


def channel(channel_id, title, **flags):
    return types.Channel(
        id=channel_id, title=title, access_hash=7,
        photo=types.ChatPhotoEmpty(), date=NOW, **flags,
    )


def topic(topic_id, title, *, closed=False):
    return types.ForumTopic(
        id=topic_id, title=title, closed=closed, date=NOW,
        peer=types.PeerChannel(101), icon_color=0, top_message=topic_id,
        read_inbox_max_id=0, read_outbox_max_id=0, unread_count=0,
        unread_mentions_count=0, unread_reactions_count=0,
        unread_poll_votes_count=0, from_id=types.PeerUser(1),
        notify_settings=types.PeerNotifySettings(),
    )


class FakeTelegram:
    def __init__(self, *, installed=True):
        self.group = channel(101, "Test Forum", forum=True, megagroup=True, creator=True)
        self.humor = channel(201, "Мемы", broadcast=True)
        self.tech = channel(202, "Python", broadcast=True, left=not installed)
        self.channels = [self.humor, self.tech]
        self.unavailable = set()
        self.omit_entities = set()
        self.installed = installed
        self.members = [self.humor.id] if not installed else [c.id for c in self.channels]
        self.topics = [topic(50, "Юмор")]
        self.joined = []
        self.creations = []
        self.reads = []
        self.history = {}
        self.available_reactions = {}
        self.fail_create = None
        self.rename_during_create = False
        self.page_size = 100
        self.drop_next_topic_page = False

    async def iter_dialogs(self):
        yield SimpleNamespace(entity=self.group)

    async def get_entity(self, peer):
        if isinstance(peer, list):
            ids = {p.channel_id for p in peer}
            if ids & self.omit_entities:
                raise KeyError(next(iter(ids & self.omit_entities)))
            return [c for c in self.channels if c.id in ids]
        if isinstance(peer, types.InputPeerChannel):
            if peer.channel_id in self.omit_entities:
                raise KeyError(peer.channel_id)
            return next(c for c in self.channels if c.id == peer.channel_id)
        if peer == "@test_forum":
            return self.group
        raise AssertionError(peer)

    async def get_messages(self, channel, *, limit):
        self.reads.append((channel.id, limit))
        if channel.id in self.unavailable:
            raise errors.ChannelPrivateError(request=None)
        posts = self.history.get(channel.id, ())[:limit]
        return [SimpleNamespace(raw_text=post) if isinstance(post, str) else post for post in posts]

    async def __call__(self, request):
        if isinstance(request, functions.chatlists.CheckChatlistInviteRequest):
            if self.installed:
                return types.chatlists.ChatlistInviteAlready(
                    13, [types.PeerChannel(c.id) for c in self.channels if c.id not in self.members],
                    [types.PeerChannel(c) for c in self.members],
                    self.channels + [self.group], [],
                )
            return types.chatlists.ChatlistInvite(
                types.TextWithEntities("Test Folder", []),
                [types.PeerChannel(c.id) for c in self.channels + [self.group]],
                self.channels + [self.group], [],
            )
        if isinstance(request, functions.chatlists.JoinChatlistInviteRequest):
            self.joined.append([p.channel_id for p in request.peers])
            self.members = self.joined[-1]
            self.installed = True
            for entity in self.channels:
                entity.left = entity.id not in self.members
            return types.Updates(updates=[], users=[], chats=[], date=NOW, seq=0)
        if isinstance(request, functions.messages.GetDialogFiltersRequest):
            filters = [types.DialogFilterChatlist(
                id=13, title=types.TextWithEntities("Test Folder", []),
                pinned_peers=[],
                include_peers=[utils.get_input_peer(c) for c in self.channels if c.id in self.members],
            )] if self.installed else []
            return types.messages.DialogFilters(filters)
        if isinstance(request, functions.channels.GetFullChannelRequest):
            return SimpleNamespace(full_chat=SimpleNamespace(
                about="", available_reactions=self.available_reactions.get(request.channel.id)
            ))
        if isinstance(request, functions.messages.GetForumTopicsRequest):
            if self.drop_next_topic_page and request.offset_topic:
                return self.topic_result([])
            offset = next((i + 1 for i, t in enumerate(self.topics)
                           if t.id == request.offset_topic), 0)
            return self.topic_result(self.topics[offset:offset + self.page_size])
        if isinstance(request, functions.messages.GetForumTopicsByIDRequest):
            return self.topic_result([t for t in self.topics if t.id in request.topics])
        if isinstance(request, functions.messages.CreateForumTopicRequest):
            self.creations.append((request.title, request.random_id))
            if self.fail_create == "before":
                raise OSError("Transport interrupted")
            if self.rename_during_create:
                self.topics[0].title = "Переименована"
            new_topic = topic(80, request.title)
            self.topics.append(new_topic)
            if self.fail_create == "after":
                raise OSError("Transport interrupted")
            return types.Updates(
                [types.UpdateMessageID(new_topic.id, request.random_id)], [], [], NOW, 0
            )
        raise AssertionError(type(request).__name__)

    def topic_result(self, topics):
        return types.messages.ForumTopics(
            count=len(self.topics), topics=topics, messages=[], chats=[], users=[], pts=0,
            order_by_create_date=True,
        )


class SetupTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.telegram = FakeTelegram()
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.db.executescript("""
            CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE sources (
                channel_id INTEGER PRIMARY KEY, title TEXT NOT NULL, username TEXT,
                category TEXT NOT NULL, topic_id INTEGER, enabled INTEGER NOT NULL DEFAULT 1,
                reaction_mode TEXT NOT NULL DEFAULT 'auto', threshold INTEGER,
                reaction_hours INTEGER NOT NULL DEFAULT 2, policy TEXT NOT NULL DEFAULT 'v2'
            );
        """)

    async def wizard(self, *answers, preview=False, link=INVITE, config=None, defer_review=False):
        output = io.StringIO()
        with patch("builtins.input", side_effect=answers), contextlib.redirect_stdout(output):
            await setup.initialize(self.telegram, self.db, config or {"group_id": 101}, link,
                                   preview=preview, defer_review=defer_review)
        return output.getvalue()

    def routes(self):
        return self.db.execute("""
            SELECT channel_id, category, topic_id, enabled, reaction_hours
            FROM sources ORDER BY channel_id
        """).fetchall()

    async def test_deferred_review_saves_suggestions_without_console_choices_or_topics(self):
        output = await self.wizard(defer_review=True)
        self.assertEqual([(201, "Юмор", None, 0, 12), (202, "Технологии", None, 0, 2)], self.routes())
        self.assertEqual([], self.telegram.creations)
        self.assertIn("/review", output)
        self.assertEqual("13", self.db.execute("SELECT value FROM settings WHERE key='folder_id'").fetchone()[0])
        await self.wizard(link=None, defer_review=True, config={
            "group_id": 101, "category_overrides": {"201": "Авторские"}
        })
        self.assertEqual("Авторские", self.routes()[0][1])

    async def test_deferred_review_preserves_existing_routes(self):
        await self.wizard("да", "", "", "да")
        original = self.routes()
        await self.wizard(link=None, defer_review=True)
        self.assertEqual(original, self.routes())

    async def test_preview_does_not_write_or_include_forum(self):
        self.telegram = FakeTelegram(installed=False)
        before = list(self.db.iterdump())
        output = await self.wizard(preview=True)
        self.assertIn("Мемы", output)
        self.assertIn("Python", output)
        self.assertNotIn("Test Forum", output)
        self.assertNotIn("synthetic-only", output)
        self.assertEqual(before, list(self.db.iterdump()))
        self.assertEqual([], self.telegram.joined)
        self.assertEqual([], self.telegram.creations)

    async def test_join_requires_selection_and_confirmation(self):
        self.telegram = FakeTelegram(installed=False)
        await self.wizard("2", "да", "да", "", "да")
        self.assertEqual([[202]], self.telegram.joined)
        self.assertEqual([(202, "Технологии", 80, 1, 2)], self.routes())

    async def test_selected_join_can_be_declined(self):
        self.telegram = FakeTelegram(installed=False)
        await self.wizard("2", "")
        self.assertEqual([], self.telegram.joined)
        self.assertEqual([], self.routes())

    async def test_reuses_exact_topic_and_preserves_manual_route_on_repeat(self):
        await self.wizard("да", "", "", "да")
        self.assertEqual([(201, "Юмор", 50, 1, 12), (202, "Технологии", 80, 1, 2)], self.routes())
        self.db.execute("UPDATE sources SET topic_id = 75, enabled = 0, category = 'Редакция' WHERE channel_id = 201")
        self.db.commit()
        await self.wizard(link=None)
        self.assertEqual([(201, "Редакция", 75, 0, 12), (202, "Технологии", 80, 1, 2)], self.routes())
        self.assertEqual(["Технологии"], [title for title, _ in self.telegram.creations])
        saved = {k: json.loads(v) for k, v in self.db.execute("SELECT key, value FROM settings")}
        self.assertEqual({"group_id": 101, "folder_id": 13}, saved)

    async def test_saved_category_takes_priority_over_dictionary_and_is_reviewed(self):
        self.telegram.topics.append(topic(60, "Инструменты"))
        config = {"group_id": 101, "category_overrides": {"202": "Инструменты"}}
        output = await self.wizard("да", "", "", "да", config=config)
        self.assertIn("категория из локального конфига", output)
        self.assertEqual([(201, "Юмор", 50, 1, 12), (202, "Инструменты", 60, 1, 2)], self.routes())
        self.assertEqual([], self.telegram.creations)

    async def test_short_flood_wait_retries_the_interrupted_history_read(self):
        original = self.telegram.get_messages
        calls = []

        async def read(channel, *, limit):
            calls.append((channel.id, limit))
            if calls == [(201, 20), (202, 20)]:
                raise errors.FloodWaitError(request=None, capture=20)
            return await original(channel, limit=limit)

        self.telegram.get_messages = read
        with patch("aggregator.setup.asyncio.sleep", new_callable=AsyncMock) as sleep:
            await self.wizard("да", "", "", "да")
        sleep.assert_awaited_once_with(20)
        self.assertEqual(calls[:3], [(201, 20), (202, 20), (202, 20)])
        self.assertEqual(len(self.routes()), 2)

    async def test_ambiguous_and_unavailable_sources_are_pending(self):
        self.telegram.humor.title = "Мемы Python"
        self.telegram.unavailable.add(202)
        await self.wizard("да", "да")
        self.assertEqual(
            [(201, "Нужно уточнить", None, 0, 2), (202, "Нужно уточнить", None, 0, 2)],
            self.routes(),
        )
        self.assertEqual([], self.telegram.creations)

    async def test_duplicate_and_closed_topics_require_explicit_open_selection(self):
        self.telegram.channels = [self.telegram.humor]
        self.telegram.topics = [topic(50, "Юмор"), topic(52, "Юмор", closed=True)]
        self.telegram.page_size = 1
        await self.wizard("да", "", "1", "да")
        self.assertEqual([(201, "Юмор", 50, 1, 12)], self.routes())
        self.assertEqual([], self.telegram.creations)

    async def test_missing_manage_topics_leaves_routes_unwritten(self):
        self.telegram.group.creator = False
        with self.assertRaisesRegex(ValueError, "manage_topics"):
            await self.wizard("да", "", "", "да")
        self.assertEqual([], self.telegram.creations)
        self.assertEqual([], self.routes())

    async def test_lost_create_response_reconciles_without_duplicate(self):
        self.telegram.fail_create = "after"
        await self.wizard("да", "", "", "да")
        self.assertEqual(1, len(self.telegram.creations))
        self.assertEqual(80, self.routes()[1][2])

    async def test_retry_after_unconfirmed_create_reuses_random_id(self):
        self.telegram.fail_create = "before"
        with self.assertRaisesRegex(ValueError, "подтверждено"):
            await self.wizard("да", "", "", "да")
        self.telegram.fail_create = None
        await self.wizard("да", "", "", "да")
        self.assertEqual(2, len(self.telegram.creations))
        self.assertEqual(self.telegram.creations[0], self.telegram.creations[1])
        self.assertEqual(80, self.routes()[1][2])

    async def test_open_topic_renamed_during_create_requires_new_confirmation(self):
        self.telegram.rename_during_create = True
        with self.assertRaisesRegex(ValueError, "Состояние тем"):
            await self.wizard("да", "", "", "да")
        self.assertEqual([], self.routes())

    async def test_incomplete_topic_list_cannot_trigger_creation(self):
        self.telegram.topics.append(topic(51, "Технологии"))
        self.telegram.page_size = 1
        self.telegram.drop_next_topic_page = True
        with self.assertRaisesRegex(ValueError, "полный список тем"):
            await self.wizard("да", "", "", "да")
        self.assertEqual([], self.telegram.creations)
        self.assertEqual([], self.routes())

    async def test_operator_can_accept_one_group_and_exclude_another(self):
        await self.wizard("нет", "да", "нет", "0", "", "да")
        self.assertEqual([(201, "Юмор", 50, 1, 12), (202, "Исключён", None, 0, 2)], self.routes())
        self.assertEqual([], self.telegram.creations)
        await self.wizard(link=None)
        self.assertEqual(0, self.routes()[1][3])

    async def test_classifier_weights_metadata_and_caps_repetition(self):
        self.telegram.history[201] = ["Python"] * 30
        self.telegram.humor.title = "ＭＥＭＥＳ"
        first = await setup.suggest_channel(self.telegram, self.telegram.humor)
        second = await setup.suggest_channel(self.telegram, self.telegram.humor)
        self.assertEqual(first, second)
        self.assertEqual("Юмор", first[0])
        self.assertEqual([(201, 20), (201, 20)], self.telegram.reads)
        self.assertTrue(any("Технологии" in item for item in first[1]))

    async def test_group_invite_is_rejected_without_echoing_secret(self):
        with self.assertRaisesRegex(ValueError, "addlist") as caught:
            await self.wizard(link="https://t.me/+synthetic-only")
        self.assertNotIn("synthetic-only", str(caught.exception))

    async def test_forced_invite_prompt_is_hidden_and_keeps_binding(self):
        self.db.execute("INSERT INTO settings VALUES ('folder_id', '14')")
        self.db.commit()
        with patch.object(setup.sys, "stdin", SimpleNamespace(isatty=lambda: True)), \
                patch.object(setup.getpass, "getpass", return_value=INVITE) as prompt:
            with self.assertRaisesRegex(ValueError, "не соответствует"):
                await self.wizard(preview=True, link="")
        prompt.assert_called_once()
        self.assertEqual([], self.telegram.joined)

    async def test_one_unresolved_folder_peer_does_not_hide_available_sources(self):
        self.telegram.omit_entities.add(202)
        self.db.execute("INSERT INTO settings VALUES ('folder_id', '13')")
        self.db.commit()
        output = await self.wizard(preview=True, link=None)
        self.assertIn("Мемы", output)
        self.assertIn("#202", output)
        self.assertEqual([201], [peer.id for peer in await setup.folder_channels(self.telegram, 13)])

    async def test_initial_reaction_estimate_counts_one_recent_post_per_album(self):
        now = datetime.now(timezone.utc)
        self.telegram.available_reactions[201] = types.ChatReactionsAll()

        def post(count, album=None, age_days=1):
            reactions = SimpleNamespace(results=[SimpleNamespace(count=count)]) if count else None
            return SimpleNamespace(raw_text="", grouped_id=album, reactions=reactions,
                                   date=now - timedelta(days=age_days))

        self.telegram.history[201] = [post(1, 77), post(9, 77), post(0), post(30, age_days=40)]
        self.telegram.history[202] = [post(100)]
        await self.wizard("да", "", "", "да")
        self.assertEqual(
            [(201, 4.5, 2), (202, None, 0)],
            self.db.execute("SELECT * FROM initial_reactions ORDER BY channel_id").fetchall(),
        )
        self.assertEqual(
            [("auto", None), ("auto", None)],
            self.db.execute("SELECT reaction_mode, threshold FROM sources ORDER BY channel_id").fetchall(),
        )
