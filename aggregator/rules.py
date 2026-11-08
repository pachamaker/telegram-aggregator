"""Deterministic publication rules."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlsplit

from telethon import helpers, types
from telethon.errors import FloodPremiumWaitError, FloodWaitError, RPCError


_URL = re.compile(r"(?:https?://|tg://|www\.|t(?:elegram)?\.me/)[^\s<>\"«»]+", re.I)
_DISCLOSURE = re.compile(
    r"(?<!\w)#(?:реклама|ad)\b"
    r"|^\s*(?:реклама|на правах рекламы|advertisement)\s*(?:$|[:.,;|—-])"
    r"|\berid\s*(?:[:=]\s*|\s+)[a-z0-9]{8,}\b",
    re.M,
)
_GIVEAWAY = re.compile(r"\b(?:розыгрыш\w*|giveaway)\b")
_SUBSCRIBE = r"(?:подписаться|подпишитесь|подпишись|subscribe)\b"
_OPTIONAL_SUBSCRIPTION = re.compile(
    r"\b(?:можно|не\s+(?:нужно|необходимо|обязательно))\s+" + _SUBSCRIBE
    + r"|\b" + _SUBSCRIBE
    + r"[^.!?\n]{0,75}\b(?:не\s*обязательно|по желанию|при желании|если хотите)\b"
)
_REQUIRED_SUBSCRIPTION = re.compile(
    r"\bобязательн\w*\s+подписк\w*\b"
    r"|\b(?:нужно|необходимо|обязательно)\s+" + _SUBSCRIBE
    + r"|\b(?:для участия|чтобы (?:участвовать|принять участие)|услови[ея] участия)"
    r"[^.!?]{0,75}\b" + _SUBSCRIBE
    + r"|^\s*(?:\d+[.)]|[-–•])\s*" + _SUBSCRIBE,
    re.M,
)
_TICKET_ACTION = r"(?:купить|заказать|забронировать|приобрести)\s+билет[ыа]?|(?:buy|book)\s+tickets?"
_TICKET_LABEL = re.compile(rf"^(?:{_TICKET_ACTION}|билеты|tickets)\s*[:—-]?$")
_TICKET_LINE = re.compile(
    rf"^\s*[🎫🎟️]*\s*({_TICKET_ACTION}|билеты|tickets)\s*[:—-]?\s*({_URL.pattern})",
    re.I | re.M,
)
_TICKET_URL = re.compile(r"(?:^|[./_-])(?:tickets?|ticketing|kassa)(?=$|[./_-])")
_EVENT_DATE = re.compile(
    r"\b\d{1,2}\s+(?:января|февраля|марта|апреля|мая|июня|июля|августа|"
    r"сентября|октября|ноября|декабря)\b"
)
_EVENT_TIME = re.compile(r"\b\d{1,2}:\d{2}\b")
_SHORT_LINK_HOSTS = {"clck.ru", "bit.ly", "t.ly", "tinyurl.com"}


def _normalize(text):
    return unicodedata.normalize("NFC", text.replace("\r\n", "\n").replace("\r", "\n"))


def _linked_entities(post, raw):
    entities = post.entities or ()
    if not entities:
        return
    # Telegram offsets count UTF-16 code units, including both halves of emoji.
    text = helpers.add_surrogate(raw)
    for entity in entities:
        if isinstance(entity, (types.MessageEntityTextUrl, types.MessageEntityUrl)):
            label = helpers.del_surrogate(text[entity.offset:entity.offset + entity.length])
            url = entity.url if isinstance(entity, types.MessageEntityTextUrl) else label
            yield entity.offset, label, url


def _button_rows(post):
    return getattr(post.reply_markup, "rows", ())


def _button_url(button):
    return getattr(getattr(button, "type", None), "url", None) or getattr(button, "url", None)


def _url_parts(url):
    try:
        return urlsplit(url if "://" in url else "//" + url)
    except ValueError:
        return urlsplit("")


def advertising_reason(posts) -> str | None:
    bodies = []
    links = []
    for post in posts:
        raw = post.message or ""
        body = _normalize(raw).casefold()
        bodies.append(body)
        links.extend((label, url) for _, label, url in _linked_entities(post, raw))
        links.extend((button.text, url) for row in _button_rows(post) for button in row.buttons
                     if (url := _button_url(button)))
        links.extend(_TICKET_LINE.findall(body))
        links.extend(("", url) for url in _URL.findall(raw))

    text = "\n".join(bodies)
    if _DISCLOSURE.search(text):
        return "ad_disclosure"
    ticket_sale = False
    for label, url in links:
        target = _url_parts(url)
        if any(key.casefold() == "erid" and re.fullmatch(r"[a-z0-9]{8,}", token, re.I)
               for key, token in parse_qsl(target.query)):
            return "ad_disclosure"
        label = _normalize(label).casefold().strip(" \t🎫🎟️")
        if not _TICKET_LABEL.fullmatch(label) or not target.hostname:
            continue
        if target.scheme in ("", "http", "https") and (
            re.match(_TICKET_ACTION, label)
            or _TICKET_URL.search((target.hostname + target.path).casefold())
            or (target.hostname.casefold() in _SHORT_LINK_HOSTS
                and _EVENT_DATE.search(text) and _EVENT_TIME.search(text))
        ):
            ticket_sale = True
    if _GIVEAWAY.search(text) and _REQUIRED_SUBSCRIPTION.search(_OPTIONAL_SUBSCRIPTION.sub("#", text)):
        return "ad_subscription_giveaway"
    if ticket_sale:
        return "ad_ticket_sale"


def _key(prefix, value):
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
    return f"{prefix}:{hashlib.sha256(encoded).hexdigest()}"


def _origin(refs, album):
    if album:
        return _key("origin-album", refs)
    channel, post_id = refs[0]
    return f"origin:{channel}:{post_id}"


def _forwarded_origin(post):
    forward = post.fwd_from
    peer = getattr(forward, "from_id", None)
    post_id = getattr(forward, "channel_post", None)
    if isinstance(peer, types.PeerChannel) and post_id:
        return peer.channel_id, post_id


@dataclass(frozen=True)
class _Attachment:
    shape: tuple
    media_id: int
    size: int
    thumb: str | None = None


def _photo_size(size):
    if isinstance(size, types.PhotoSize):
        return size.size
    if isinstance(size, types.PhotoSizeProgressive):
        return max(size.sizes, default=0)
    return 0


def _attachment(media):
    if isinstance(media, types.MessageMediaPhoto) and isinstance(media.photo, types.Photo):
        if media.ttl_seconds or media.live_photo or media.video:
            return None
        photo = media.photo
        size, thumb = max(
            ((_photo_size(size), size.type) for size in photo.sizes),
            default=(0, None),
        )
        return _Attachment(("photo", bool(media.spoiler)), photo.id, size, thumb)
    if isinstance(media, types.MessageMediaDocument) and isinstance(media.document, types.Document):
        if media.ttl_seconds or media.video_cover:
            return None
        doc = media.document
        shape = (
            "document", doc.mime_type, bool(media.spoiler), bool(media.video),
            bool(media.round), bool(media.voice), media.video_timestamp,
            [bytes(attribute).hex() for attribute in doc.attributes],
        )
        return _Attachment(shape, doc.id, doc.size)


def _linked_targets(post, raw):
    text = helpers.add_surrogate(raw)
    targets = []
    for offset, label, url in _linked_entities(post, raw):
        if url != label:
            start = len(_normalize(helpers.del_surrogate(text[:offset])))
            targets.append((start, len(_normalize(label)), url))
    for entity in post.entities or ():
        if isinstance(entity, (types.MessageEntityMentionName, types.MessageEntityCustomEmoji)):
            start = len(_normalize(helpers.del_surrogate(text[:entity.offset])))
            if isinstance(entity, types.MessageEntityMentionName):
                targets.append((start, entity.length, f"tg://user?id={entity.user_id}"))
            else:
                targets.append((start, entity.length, f"tg://emoji?id={entity.document_id}"))
    return sorted(targets)


class _MediaDigest:
    def __init__(self, size):
        self.size = size
        self.received = 0
        self.hash = hashlib.sha256()

    def write(self, chunk):
        self.received += len(chunk)
        if self.received > self.size:
            raise OSError("Telegram returned more media bytes than declared")
        self.hash.update(chunk)
        return len(chunk)


async def fingerprint(client, posts, source_id, *, max_media_bytes=20 * 1024 * 1024) -> list[str]:
    posts = sorted(posts, key=lambda post: post.id)
    if not posts:
        return []
    album = len(posts) > 1 or bool(posts[0].grouped_id)
    keys = {_origin([(source_id, post.id) for post in posts], album)}
    provenance = [_forwarded_origin(post) for post in posts]
    if all(provenance):
        keys.add(_origin(provenance, album))

    parts = []
    attachments = []
    has_content = False
    for post in posts:
        if getattr(post, "action", None) or getattr(post, "rich_message", None):
            return sorted(keys)
        raw = post.message or ""
        body = _normalize(raw)
        markup = [[(button.text, _button_url(button)) for button in row.buttons]
                  for row in _button_rows(post)]
        if post.reply_markup and (not markup or any(not url for row in markup for _, url in row)):
            return sorted(keys)
        attachment = None
        manual_preview = None
        media = post.media
        if isinstance(media, types.MessageMediaWebPage):
            if media.manual:
                manual_preview = getattr(media.webpage, "url", None)
                if not manual_preview:
                    return sorted(keys)
        elif media is not None and not isinstance(media, types.MessageMediaEmpty):
            attachment = _attachment(media)
            if attachment is None:
                return sorted(keys)
        parts.append((body, _linked_targets(post, raw), markup, manual_preview))
        attachments.append(attachment)
        has_content |= bool(attachment or _URL.sub("", body).strip())

    if not has_content:
        return sorted(keys)
    ids = [(item.shape, item.media_id) if item else None for item in attachments]
    keys.add(_key("content", (album, parts, ids)))

    files = [item for item in attachments if item is not None]
    if (
        not client or not files or any(item.size <= 0 for item in files)
        or sum(item.size for item in files) > max_media_bytes
        or any(post.noforwards or getattr(post.chat, "noforwards", False) for post in posts)
    ):
        return sorted(keys)

    digests = []
    for post, item in zip(posts, attachments):
        if not item:
            digests.append(None)
            continue
        sink = _MediaDigest(item.size)
        try:
            await client.download_media(post, file=sink, thumb=item.thumb)
        except (FloodWaitError, FloodPremiumWaitError):
            raise
        except (OSError, RPCError, ValueError):
            return sorted(keys)
        if sink.received != item.size:
            return sorted(keys)
        digests.append((item.shape, sink.hash.hexdigest()))
    keys.add(_key("content-bytes", (album, parts, digests)))
    return sorted(keys)


def reaction_count(posts) -> int:
    return max(
        (sum(result.count for result in getattr(post.reactions, "results", ())) for post in posts),
        default=0,
    )


def reaction_availability(full_chat) -> bool | None:
    available = getattr(full_chat, "available_reactions", None)
    if getattr(full_chat, "paid_reactions_available", False) or isinstance(available, types.ChatReactionsAll):
        return True
    if isinstance(available, types.ChatReactionsSome):
        return bool(available.reactions)
    if isinstance(available, types.ChatReactionsNone):
        return False
