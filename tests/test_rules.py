import unittest
from datetime import datetime, timezone
from types import SimpleNamespace

from telethon import types

from aggregator.rules import (
    advertising_reason,
    fingerprint,
    reaction_availability,
    reaction_count,
)


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def message(id=1, text="", **kwargs):
    return types.Message(id=id, peer_id=types.PeerChannel(10), date=NOW, message=text, **kwargs)


def hidden_link(text, label, url):
    start = text.index(label)
    return types.MessageEntityTextUrl(
        len(text[:start].encode("utf-16-le")) // 2,
        len(label.encode("utf-16-le")) // 2,
        url,
    )


def document(id, size=5):
    return types.MessageMediaDocument(
        document=types.Document(id, 0, b"", NOW, "application/octet-stream", size, 1, [])
    )


def photo(id, size=5):
    return types.MessageMediaPhoto(photo=types.Photo(
        id, 0, b"", NOW, [types.PhotoSize("x", 100, 100, size)], 1
    ))


def forward(channel, id):
    return types.MessageFwdHeader(NOW, from_id=types.PeerChannel(channel), channel_post=id)


class MediaClient:
    def __init__(self, files=()):
        self.files = dict(files)
        self.downloaded = []

    async def download_media(self, post, file, *, thumb=None):
        self.downloaded.append(post.id)
        data = self.files[post.id]
        if isinstance(data, OSError):
            raise data
        file.write(data[:2])
        file.write(data[2:])
        return file


class AdvertisingTests(unittest.TestCase):
    def test_disclosure_and_erid_in_hidden_destination(self):
        for posts in (
            [message(text="Реклама. ООО «Пример»")],
            [message(text="Описание\n#реклама")],
            [message(text="Описание"), message(2, "Подробнее", entities=[
                hidden_link("Подробнее", "Подробнее", "https://example.com/p?erid=Abc123456")
            ])],
        ):
            with self.subTest(posts=posts):
                self.assertEqual(advertising_reason(posts), "ad_disclosure")

    def test_subscription_required_for_giveaway(self):
        for text in (
            "Розыгрыш книг! Для участия подпишитесь на канал @example.",
            "Розыгрыш\n1. Подписаться на канал @example\n2. Поставить реакцию",
            "Условие розыгрыша — обязательная подписка.",
        ):
            with self.subTest(text=text):
                self.assertEqual(advertising_reason([message(text=text)]), "ad_subscription_giveaway")

    def test_giveaway_does_not_require_subscription_if_conditions_say_it_is_optional(self):
        for text in (
            "Розыгрыш! Для участия можно подписаться или оставить комментарий.",
            "Розыгрыш открыт для всех. Не нужно подписываться и не нужно подписаться на канал.",
            "Розыгрыш! Условия участия:\n1. Подписаться на канал необязательно.",
        ):
            with self.subTest(text=text):
                self.assertIsNone(advertising_reason([message(text=text)]))

    def test_ticket_sale_uses_link_label_and_destination(self):
        for post in (
            message(text="Концерт 20 мая\nКупить билеты: https://example.com/concert"),
            message(text="Афиша. 🎫 Купить билеты", entities=[
                hidden_link("Афиша. 🎫 Купить билеты", "Купить билеты", "https://example.com/concert")
            ]),
            message(text="Театр 20 мая\nБилеты: https://example.com/tickets/20"),
        ):
            with self.subTest(post=post):
                self.assertEqual(advertising_reason([post]), "ad_ticket_sale")

    def test_scheduled_event_with_shortened_ticket_link(self):
        text = "Концерт\n20 ноября в 19:00\n🎫 БИЛЕТЫ"
        link = hidden_link(text, "БИЛЕТЫ", "https://clck.ru/sample")
        self.assertEqual(advertising_reason([message(text=text, entities=[link])]), "ad_ticket_sale")
        label = "БИЛЕТЫ"
        self.assertIsNone(advertising_reason([
            message(text=label, entities=[hidden_link(label, label, "https://clck.ru/sample")])
        ]))

    def test_editorial_content_and_optional_subscription_pass(self):
        for text in (
            "Участники обсудили рекламу, скидки и выбор спонсора.",
            "Розыгрыш уже прошёл. Можно подписаться на канал, если хотите.",
            "Как купить билеты дешевле: https://example.com/report",
            "Городская афиша. Билеты поступят в продажу в июне.",
            "Купить билеты пока нельзя. Источник: https://example.com/report",
            "Реклама в городской среде стала темой лекции.",
        ):
            with self.subTest(text=text):
                self.assertIsNone(advertising_reason([message(text=text)]))


class FingerprintTests(unittest.IsolatedAsyncioTestCase):
    async def assert_match(self, first, second, *, client=None, max_media_bytes=0):
        first_keys = await fingerprint(client, first, 10, max_media_bytes=max_media_bytes)
        second_keys = await fingerprint(client, second, 20, max_media_bytes=max_media_bytes)
        self.assertTrue(set(first_keys) & set(second_keys))

    async def assert_distinct(self, first, second, *, client=None, max_media_bytes=0):
        first_keys = await fingerprint(client, first, 10, max_media_bytes=max_media_bytes)
        second_keys = await fingerprint(client, second, 20, max_media_bytes=max_media_bytes)
        self.assertFalse(set(first_keys) & set(second_keys))

    async def test_original_and_forward_share_legacy_compatible_origin_key(self):
        original = await fingerprint(None, [message(3, "Оригинал")], 10)
        repost = await fingerprint(None, [message(5, "Репост", fwd_from=forward(10, 3))], 20)
        self.assertEqual(set(original) & set(repost), {"origin:10:3"})

    async def test_full_album_origin_matches_but_a_shared_part_does_not(self):
        original = [
            message(3, "Подпись", media=photo(1), grouped_id=1),
            message(4, media=photo(2), grouped_id=1),
        ]
        repost = [
            message(7, media=photo(4), grouped_id=2, fwd_from=forward(10, 4)),
            message(6, "Подпись", media=photo(3), grouped_id=2, fwd_from=forward(10, 3)),
        ]
        await self.assert_match(original, repost)
        await self.assert_distinct(original, repost[:1])
        repost[0].fwd_from = forward(10, 8)
        await self.assert_distinct(original, repost)

    async def test_shared_photo_with_new_caption_is_distinct(self):
        await self.assert_distinct(
            [message(text="Новость", media=photo(1))],
            [message(text="Комментарий", media=photo(1))],
        )

    async def test_complete_telegram_media_identity_preserves_order_and_captions(self):
        original = [
            message(3, "Первый", media=photo(1), grouped_id=1),
            message(4, "Второй", media=photo(2), grouped_id=1),
        ]
        copy = [
            message(7, "Второй", media=photo(2), grouped_id=2),
            message(6, "Первый", media=photo(1), grouped_id=2),
        ]
        await self.assert_match(original, copy)
        copy[0].message = "Другая подпись"
        await self.assert_distinct(original, copy)
        copy[0].message = "Второй"
        copy[0].id = 5
        await self.assert_distinct(original, copy)

    async def test_hidden_link_target_is_part_of_identity(self):
        caption = "🎫 Открыть материал"
        first = message(text=caption, entities=[
            hidden_link(caption, "Открыть", "https://example.com/a")
        ])
        second = message(text=caption, entities=[
            hidden_link(caption, "Открыть", "https://example.com/b")
        ])
        await self.assert_distinct([first], [second])

    async def test_nfc_and_line_endings_normalize_without_losing_link_position(self):
        first = "🎫 й\r\nОткрыть"
        second = "🎫 и\u0306\nОткрыть"
        await self.assert_match(
            [message(text=first, entities=[hidden_link(first, "Открыть", "https://example.com/a")])],
            [message(text=second, entities=[hidden_link(second, "Открыть", "https://example.com/a")])],
        )

    async def test_common_link_and_rewrites_do_not_match(self):
        await self.assert_distinct(
            [message(text="https://example.com/story")],
            [message(text="https://example.com/story")],
        )
        await self.assert_distinct(
            [message(text="Совет провёл встречу https://example.com/story")],
            [message(text="Представители встретились https://example.com/story")],
        )

    async def test_unsupported_attachment_does_not_match_as_caption_only(self):
        await self.assert_distinct(
            [message(text="Варианты?", media=types.MessageMediaUnsupported())],
            [message(text="Варианты?", media=types.MessageMediaUnsupported())],
        )

    async def test_equal_bytes_match_independently_uploaded_media(self):
        for media in (document, photo):
            with self.subTest(media=media.__name__):
                client = MediaClient({1: b"abcde", 2: b"abcde", 3: b"abxyz"})
                first = [message(1, "Подпись", media=media(1))]
                copy = [message(2, "Подпись", media=media(2))]
                different = [message(3, "Подпись", media=media(3))]
                await self.assert_match(first, copy, client=client, max_media_bytes=5)
                await self.assert_distinct(first, different, client=client, max_media_bytes=5)

    async def test_protected_or_over_budget_album_uses_telegram_ids_without_downloads(self):
        client = MediaClient()
        original = [
            message(1, media=document(1), grouped_id=1),
            message(2, media=document(2), grouped_id=1),
        ]
        await self.assert_match(original, original, client=client, max_media_bytes=9)
        original[1].noforwards = True
        await self.assert_match(original, original, client=client, max_media_bytes=10)
        self.assertEqual(client.downloaded, [])

    async def test_embedded_photo_preview_is_not_proof_of_equal_original_bytes(self):
        first = photo(1)
        second = photo(2)
        for attachment in (first, second):
            attachment.photo.sizes = [types.PhotoCachedSize("s", 90, 90, b"abcde")]
        client = MediaClient({1: b"abcde", 2: b"abcde"})
        await self.assert_distinct(
            [message(1, media=first)], [message(2, media=second)], client=client, max_media_bytes=5,
        )

    async def test_download_error_or_wrong_byte_count_cannot_prove_a_match(self):
        first = [message(1, media=document(1))]
        second = [message(2, media=document(2))]
        for incomplete in (OSError("unavailable"), b"abc", b"abcdef"):
            with self.subTest(incomplete=incomplete):
                client = MediaClient({1: incomplete, 2: incomplete})
                await self.assert_distinct(first, second, client=client, max_media_bytes=5)


class ReactionTests(unittest.TestCase):
    def test_album_uses_maximum_sum_of_reactions_per_part(self):
        first = message(reactions=types.MessageReactions([
            types.ReactionCount(types.ReactionEmoji("👍"), 3),
            types.ReactionCount(types.ReactionEmoji("❤️"), 2),
        ]))
        second = message(2, reactions=types.MessageReactions([
            types.ReactionCount(types.ReactionEmoji("👍"), 4),
        ]))
        self.assertEqual(reaction_count([first, second, message(3)]), 5)
        self.assertEqual(reaction_count([message()]), 0)

    def test_reaction_availability_distinguishes_enabled_disabled_and_unknown(self):
        for settings, expected in (
            (types.ChatReactionsAll(), True),
            (types.ChatReactionsSome([types.ReactionEmoji("👍")]), True),
            (types.ChatReactionsSome([]), False),
            (types.ChatReactionsNone(), False),
            (None, None),
        ):
            with self.subTest(settings=settings):
                self.assertIs(reaction_availability(SimpleNamespace(available_reactions=settings)), expected)

    def test_paid_reactions_can_be_enabled_independently(self):
        full = SimpleNamespace(
            available_reactions=types.ChatReactionsNone(), paid_reactions_available=True,
        )
        self.assertIs(reaction_availability(full), True)
