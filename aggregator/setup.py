"""Interactive setup for a shared Telegram folder and a forum."""

import asyncio
import getpass
import json
import re
import secrets
import statistics
import sys
import unicodedata
import warnings
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from telethon import errors, functions, types, utils

from .rules import reaction_availability, reaction_count

PENDING_CATEGORY = "Нужно уточнить"
EXCLUDED_CATEGORY = "Исключён"
ACCESS_ERRORS = (
    errors.ChannelPrivateError,
    errors.ChannelInvalidError,
    errors.ChannelPublicGroupNaError,
    errors.UserBannedInChannelError,
    errors.UserNotParticipantError,
    errors.ChatAdminRequiredError,
    errors.PeerIdInvalidError,
)
RPC_FAILURES = (errors.RPCError, OSError, asyncio.TimeoutError)
READ_RATE_LIMITS = (errors.FloodWaitError, errors.FloodPremiumWaitError)
MAX_READ_WAIT_SECONDS = 60
MAX_READ_RETRIES = 3


@dataclass
class _Candidate:
    channel: types.Channel
    category: Optional[str]
    evidence: list
    readable: bool
    reaction_estimate: tuple


async def _read(call, *args, wait=True, **kwargs):
    """Retry a wizard read while Telegram requests a short pause."""
    for attempt in range(MAX_READ_RETRIES + 1):
        try:
            return await call(*args, **kwargs)
        except READ_RATE_LIMITS as exc:
            if not wait or exc.seconds > MAX_READ_WAIT_SECONDS or attempt == MAX_READ_RETRIES:
                raise
            print(f"Telegram просит подождать {exc.seconds} с. Затем продолжу чтение.", flush=True)
            await asyncio.sleep(exc.seconds)


def _normalize(text):
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def _visible(text):
    return "".join(c for c in text if not unicodedata.category(c).startswith("C"))


def _label(channel):
    return f"{_visible(channel.title)} [#{channel.id}]"


def _invite_slug(link):
    if not link:
        if not sys.stdin.isatty():
            raise ValueError("Введите ссылку на общую папку в интерактивном терминале.")
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            try:
                link = getpass.getpass("Ссылка на общую папку Telegram (скрытый ввод): ")
            except getpass.GetPassWarning:
                raise ValueError("Терминал не поддерживает скрытый ввод ссылки.") from None
            except EOFError:
                raise ValueError("Ввод ссылки завершён.") from None
    match = re.fullmatch(r"(?:https://)?t\.me/addlist/([A-Za-z0-9_-]+)", link.strip())
    if match is None:
        raise ValueError("Укажите ссылку на общую папку в формате t.me/addlist/…")
    return match[1]


async def resolve_group(client, group_id):
    """Resolve a target forum without confusing a bare channel ID with a user ID."""
    group = None
    if isinstance(group_id, str) and re.fullmatch(r"@[A-Za-z0-9_]+", group_id):
        try:
            group = await client.get_entity(group_id)
        except ValueError:
            pass
    elif isinstance(group_id, int) and not isinstance(group_id, bool) and group_id > 0:
        async for dialog in client.iter_dialogs():
            if isinstance(dialog.entity, types.Channel) and dialog.entity.id == group_id:
                group = dialog.entity
                break
    if (not isinstance(group, types.Channel) or not group.megagroup or not group.forum
            or group.broadcast or group.left or getattr(group, "monoforum", False)):
        raise ValueError("Цель должна быть доступной супергруппой с включёнными темами.")
    return group


def _available_broadcast(entity):
    return (isinstance(entity, types.Channel) and entity.broadcast and not entity.megagroup
            and not entity.left and not entity.min and entity.access_hash is not None)


async def _folder_entries(client, folder_id, *, wait=False):
    result = await _read(client, functions.messages.GetDialogFiltersRequest(), wait=wait)
    for folder in result.filters:
        if isinstance(folder, types.DialogFilterChatlist) and folder.id == folder_id:
            break
    else:
        raise ValueError("Общая папка не найдена в пользовательской сессии Telegram.")
    peers = {
        peer.channel_id: peer
        for peer in [*folder.pinned_peers, *folder.include_peers]
        if isinstance(peer, types.InputPeerChannel)
    }
    if not peers:
        return {}
    try:
        entities = await _read(client.get_entity, list(peers.values()), wait=wait)
    except (*ACCESS_ERRORS, KeyError):
        # Telethon's bulk resolver raises KeyError if Telegram omits one requested peer.
        entities = []
        for peer in peers.values():
            try:
                entities.append(await _read(client.get_entity, peer, wait=wait))
            except (*ACCESS_ERRORS, KeyError):
                continue
    known = {entity.id: entity for entity in entities}
    return {channel_id: known.get(channel_id) for channel_id in sorted(peers)}


async def folder_channels(client, folder_id, *, wait=False):
    """Read attached broadcast channels from one shared dialog filter."""
    entries = await _folder_entries(client, folder_id, wait=wait)
    return [entity for entity in entries.values() if _available_broadcast(entity)]


async def _check_invite(client, slug):
    try:
        return await _read(client, functions.chatlists.CheckChatlistInviteRequest(slug))
    except READ_RATE_LIMITS:
        raise
    except RPC_FAILURES as exc:
        raise ValueError(f"Не удалось проверить ссылку на папку ({type(exc).__name__}).") from None


def _invite_entries(invite, group_id):
    if isinstance(invite, types.chatlists.ChatlistInviteAlready):
        peers = [*invite.already_peers, *invite.missing_peers]
        missing = {p.channel_id for p in invite.missing_peers if isinstance(p, types.PeerChannel)}
    elif isinstance(invite, types.chatlists.ChatlistInvite):
        peers, missing = invite.peers, set()
    else:
        raise ValueError("Telegram вернул неподдерживаемый формат приглашения в папку.")

    chats = {chat.id: chat for chat in invite.chats
             if isinstance(chat, (types.Channel, types.ChannelForbidden))}
    entries = {}
    for peer in peers:
        if not isinstance(peer, types.PeerChannel) or peer.channel_id == group_id:
            continue
        entity = chats.get(peer.channel_id)
        if entity is None:
            entries[peer.channel_id] = (None, "недоступен")
        elif entity.broadcast and not entity.megagroup:
            if isinstance(entity, types.ChannelForbidden) or entity.min or entity.access_hash is None:
                status = "недоступен"
            elif entity.left or entity.id in missing:
                status = "требует вступления или добавления в папку"
            else:
                status = "доступен"
            entries[peer.channel_id] = (entity, status)
    return entries


async def _history(client, channel, limit, *, wait=True):
    try:
        return await _read(client.get_messages, channel, limit=limit, wait=wait)
    except ACCESS_ERRORS:
        return None


def _reaction_estimate(full_chat, posts):
    if reaction_availability(full_chat) is not True:
        return None, 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    samples = {}
    for index, post in enumerate(posts):
        if isinstance(post, (types.MessageService, types.MessageEmpty)):
            continue
        if getattr(post, "date", None) is None or post.date < cutoff:
            continue
        key = ("album", post.grouped_id) if post.grouped_id is not None else ("post", index)
        samples[key] = max(samples.get(key, 0), reaction_count([post]))
    return (statistics.median(samples.values()), len(samples)) if samples else (None, 0)


async def _classify(client, channel, *, wait=False, category_override=None):
    full_chat = None
    try:
        result = await _read(client, functions.channels.GetFullChannelRequest(channel=channel), wait=wait)
        full_chat = result.full_chat
    except ACCESS_ERRORS:
        pass
    messages = await _history(client, channel, 20, wait=wait)
    estimate = _reaction_estimate(full_chat, messages or ())
    if category_override is not None:
        evidence = ["категория из локального конфига"]
        if messages is None:
            evidence.append("история недоступна")
        return _Candidate(channel, category_override, evidence, messages is not None, estimate)

    dictionary = json.loads(Path(__file__).with_name("categories.json").read_text(encoding="utf-8"))
    title = _normalize(channel.title)
    description = _normalize(getattr(full_chat, "about", "") or "")
    posts = [_normalize(getattr(post, "raw_text", "") or "") for post in (messages or ())]
    ranked = []
    for category, keywords in dictionary["categories"].items():
        score, matches = 0, []
        for keyword, weight in keywords.items():
            term = re.compile(r"(?<!\w)" + re.escape(_normalize(keyword)) + r"(?!\w)")
            for field, texts, factor in (
                ("название", [title], 5), ("описание", [description], 3), ("посты", posts, 2)
            ):
                if any(term.search(text) for text in texts):
                    points = weight * factor
                    score += points
                    matches.append(f"{keyword} ({field}, +{points})")
        ranked.append((score, category, matches))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    evidence = [f"словарь v{dictionary['version']}"]
    for score, category, matches in ranked[:2]:
        evidence.append(f"{category}: {score}" + (f" — {', '.join(matches)}" if matches else ""))
    score, category, _ = ranked[0]
    runner_up = ranked[1][0] if len(ranked) > 1 else 0
    if score < dictionary["minimum_score"] or score - runner_up < dictionary["minimum_margin"]:
        category = None
    if messages is None:
        evidence.append("история недоступна")
    return _Candidate(channel, category, evidence, messages is not None, estimate)


async def suggest_channel(client, channel):
    candidate = await _classify(client, channel)
    return candidate.category, candidate.evidence


def _yes(prompt):
    return input(f"{prompt} [да/нет]: ").strip().casefold() in {"да", "д", "yes", "y"}


def _select_join(choices):
    for index, entity in enumerate(choices, 1):
        print(f"  {index}. {_label(entity)}")
    while True:
        answer = input("Каналы для подключения: номера через запятую, «все» или Enter для пропуска: ")
        answer = answer.strip().casefold()
        if not answer:
            return []
        if answer in {"все", "all"}:
            return choices
        try:
            numbers = {int(part) for part in answer.split(",")}
        except ValueError:
            numbers = set()
        if numbers and all(1 <= n <= len(choices) for n in numbers):
            return [choices[n - 1] for n in sorted(numbers)]
        print("Укажите только номера из списка.")


async def _choose_folder(client, folder_id, folder_link, group_id, preview):
    if folder_link is None and folder_id is not None:
        entries = await _folder_entries(client, folder_id, wait=True)
        channels = []
        for channel_id, channel in entries.items():
            if channel_id == group_id or (channel is not None and not getattr(channel, "broadcast", False)):
                continue
            if _available_broadcast(channel):
                channels.append(channel)
                if not preview:
                    continue
                status = "доступен" if await _history(client, channel, 1) is not None else "недоступен"
            else:
                status = "недоступен"
            label = _label(channel) if channel is not None else f"#{channel_id}"
            print(f"  {label} — {status}")
        return folder_id, channels

    slug = _invite_slug(folder_link)
    invite = await _check_invite(client, slug)
    attached_id = getattr(invite, "filter_id", None)
    if folder_id is not None and attached_id != folder_id:
        raise ValueError("Ссылка не соответствует настроенной общей папке.")
    entries = _invite_entries(invite, group_id)
    print("Вещательные каналы в приглашении:")
    for channel_id, (entity, status) in entries.items():
        label = _label(entity) if entity is not None else f"#{channel_id}"
        if preview and status == "доступен" and await _history(client, entity, 1) is None:
            status = "недоступен"
        print(f"  {label} — {status}")
    if not entries:
        print("Подходящих каналов нет.")
    if preview:
        return attached_id, []

    choices = [entity for entity, status in entries.values()
               if status != "недоступен" and (attached_id is None or status != "доступен")]
    selected = _select_join(choices) if choices else []
    if selected:
        print("Telegram подключит: " + ", ".join(_label(entity) for entity in selected))
    if selected and _yes("Подтвердить подключение к папке"):
        try:
            await client(functions.chatlists.JoinChatlistInviteRequest(
                slug=slug, peers=[utils.get_input_peer(entity) for entity in selected]
            ))
        except RPC_FAILURES:
            pass  # A lost response can mean the import succeeded. Read back before any retry.
        actual = await _check_invite(client, slug)
        actual_id = getattr(actual, "filter_id", None)
        if actual_id is None or (attached_id is not None and attached_id != actual_id):
            raise ValueError("Подключение папки не подтвердилось. Проверьте её в Telegram и повторите init.")
        attached_id = actual_id
        channels = await folder_channels(client, attached_id, wait=True)
        missing_ids = {entity.id for entity in selected} - {entity.id for entity in channels}
        if missing_ids:
            print("После подключения недоступны: " + ", ".join(
                _label(entity) for entity in selected if entity.id in missing_ids
            ))
        return attached_id, channels
    return (attached_id, await folder_channels(client, attached_id, wait=True)) if attached_id is not None else (None, [])


def _open_topic(topic):
    return isinstance(topic, types.ForumTopic) and topic.id > 1 and not topic.closed and not topic.hidden


def _in_forum(topic, group):
    return isinstance(topic.peer, types.PeerChannel) and topic.peer.channel_id == group.id


async def _topics(client, group):
    topics, offsets = {}, set()
    offset = (None, 0, 0)
    while offset not in offsets:
        offsets.add(offset)
        result = await _read(client, functions.messages.GetForumTopicsRequest(group, *offset, limit=100))
        topics.update((topic.id, topic) for topic in result.topics
                      if isinstance(topic, types.ForumTopic) and _in_forum(topic, group))
        if len(topics) >= result.count:
            return list(topics.values())
        if not result.topics:
            break
        last = result.topics[-1]
        if not isinstance(last, types.ForumTopic) or not _in_forum(last, group):
            break
        message_date = next((getattr(msg, "date", None) for msg in result.messages
                             if msg.id == last.top_message), None)
        offset = (message_date, last.top_message, last.id)
    raise ValueError("Не удалось прочитать полный список тем форума.")


async def _verified_topics(client, group, ids):
    if not ids:
        return {}
    result = await _read(client, functions.messages.GetForumTopicsByIDRequest(group, sorted(ids)))
    return {topic.id: topic for topic in result.topics
            if _open_topic(topic) and _in_forum(topic, group) and topic.id in ids}


async def list_open_topics(client, group):
    return [(topic.id, _visible(topic.title)) for topic in await _topics(client, group)
            if _open_topic(topic)]


def _topic_plan(topics, categories):
    open_topics = {i: topic for i, topic in enumerate(filter(_open_topic, topics), 1)}
    if open_topics:
        print("Открытые темы:")
        for number, topic in open_topics.items():
            print(f"  {number}. {_visible(topic.title)} [#{topic.id}]")
    plan = {}
    for category in categories:
        matches = [topic for topic in topics if _normalize(topic.title) == _normalize(category)]
        if not matches:
            default = "создать тему"
        elif len(matches) == 1 and _open_topic(matches[0]):
            default = f"использовать «{_visible(matches[0].title)}» [#{matches[0].id}]"
        else:
            default = "совпадение неоднозначно или тема закрыта"
        while True:
            answer = input(f"{category} — {default}. Номер открытой темы или 0 для пропуска [Enter]: ").strip()
            if answer == "0":
                break
            if answer.isascii() and answer.isdigit() and len(answer) <= 9:
                chosen = open_topics.get(int(answer))
                if chosen is not None:
                    plan[category] = chosen.id
                    break
            if not answer and (not matches or len(matches) == 1 and _open_topic(matches[0])):
                plan[category] = matches[0].id if matches else None
                break
            print("Выберите номер открытой темы или 0 для пропуска.")
    return plan


async def ensure_category_topic(client, db, group, title):
    key = (group.id, _normalize(title))
    matches = [topic for topic in await _topics(client, group)
               if _normalize(topic.title) == key[1]]
    if matches:
        if len(matches) == 1 and _open_topic(matches[0]):
            return matches[0].id
        raise ValueError(f"Тема «{title}» появилась или закрылась. Повторите выбор темы.")
    with db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS setup_topic_attempts (
                group_id INTEGER NOT NULL, title TEXT NOT NULL,
                random_id INTEGER NOT NULL, topic_id INTEGER,
                PRIMARY KEY (group_id, title)
            )
        """)
        db.execute("INSERT OR IGNORE INTO setup_topic_attempts VALUES (?, ?, ?, NULL)",
                   (*key, secrets.randbits(63) or 1))
        random_id, prior_id = db.execute(
            "SELECT random_id, topic_id FROM setup_topic_attempts WHERE group_id = ? AND title = ?", key
        ).fetchone()
    if prior_id is not None:
        raise ValueError(f"Ранее созданная тема «{title}» изменилась. Выберите существующую тему.")

    updates = ()
    try:
        response = await client(functions.messages.CreateForumTopicRequest(
            peer=group, title=title, random_id=random_id
        ))
        updates = getattr(response, "updates", ())
    except (errors.ChatAdminRequiredError, errors.ChatWriteForbiddenError):
        raise ValueError(f"Для темы «{title}» нужно право manage_topics или ручное создание.") from None
    except RPC_FAILURES:
        pass  # Check the forum before deciding whether the request took effect.

    confirmed_ids = {update.id for update in updates
                     if isinstance(update, types.UpdateMessageID) and update.random_id == random_id}
    matches = [topic for topic in await _topics(client, group)
               if _normalize(topic.title) == key[1]]
    if (len(matches) != 1 or not _open_topic(matches[0])
            or confirmed_ids and confirmed_ids != {matches[0].id}):
        raise ValueError(f"Создание темы «{title}» не подтверждено. Проверьте форум и повторите init.")
    topic_id = matches[0].id
    with db:
        db.execute("UPDATE setup_topic_attempts SET topic_id = ? WHERE group_id = ? AND title = ?",
                   (topic_id, *key))
    return topic_id


def _settings(db):
    return {key: json.loads(value) for key, value in db.execute(
        "SELECT key, value FROM settings WHERE key IN ('group_id', 'folder_id')"
    )}


def _check_binding(saved, group_id, folder_id):
    if (saved.get("group_id", group_id) != group_id
            or folder_id is not None and saved.get("folder_id") not in (None, folder_id)):
        raise ValueError("Этот экземпляр уже привязан к другому форуму или папке.")


def _review_categories(groups):
    print("Предложения по категориям:")
    for category, entries in groups.items():
        print(f"  {category or PENDING_CATEGORY}")
        for candidate in entries:
            print(f"    {_label(candidate.channel)} — {'; '.join(candidate.evidence)}")
            estimate, count = candidate.reaction_estimate
            if count:
                print(f"      Медиана реакций по {count} публикациям: {estimate:g} (справочно)")

    keep_suggestions = _yes("Принять предложенные категории для доступных каналов")
    routes = []
    for category, entries in groups.items():
        keep_group = keep_suggestions or bool(category and _yes(f"Принять группу «{category}»"))
        for candidate in entries:
            chosen = category if candidate.readable else None
            if candidate.readable and not keep_group:
                while True:
                    label = _label(candidate.channel)
                    answer = input(f"{label}: название категории, 0 исключить, Enter оставить на разбор: ")
                    answer = " ".join(answer.split())
                    if not answer:
                        chosen = None
                        break
                    if answer == "0":
                        chosen = EXCLUDED_CATEGORY
                        break
                    if len(answer) <= 128 and answer == _visible(answer) and _normalize(answer) not in {
                        _normalize(PENDING_CATEGORY), _normalize(EXCLUDED_CATEGORY)
                    }:
                        chosen = answer
                        break
                    print("Введите название категории длиной до 128 символов.")
            routes.append((candidate, chosen))
    return routes


def _persist(db, group_id, folder_id, routes, plan, *, defer_review=False):
    with db:
        db.executemany("""
            INSERT INTO settings(key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value WHERE settings.value='null'
        """,
                       [("group_id", json.dumps(group_id)), ("folder_id", json.dumps(folder_id))])
        _check_binding(_settings(db), group_id, folder_id)
        db.execute("""
            CREATE TABLE IF NOT EXISTS setup_source_suggestions (
                channel_id INTEGER PRIMARY KEY, evidence TEXT NOT NULL
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS initial_reactions (
                channel_id INTEGER PRIMARY KEY, threshold REAL, sample_size INTEGER NOT NULL
            )
        """)
        for candidate, category in routes:
            channel = candidate.channel
            topic_id = plan.get(category)
            stored_category = category or PENDING_CATEGORY
            enabled = int(topic_id is not None)
            hours = 12 if _normalize(stored_category) == _normalize("Юмор") else 2
            db.execute("""
                INSERT INTO sources(channel_id, title, username, category, topic_id, enabled, reaction_hours)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(channel_id) DO UPDATE SET
                    category = excluded.category, topic_id = excluded.topic_id, enabled = excluded.enabled
                WHERE sources.topic_id IS NULL AND sources.category != ?
                  AND (? OR excluded.topic_id IS NOT NULL OR excluded.category = ?)
            """, (channel.id, channel.title, channel.username, stored_category, topic_id, enabled,
                  hours, EXCLUDED_CATEGORY, defer_review, EXCLUDED_CATEGORY))
            db.execute("""
                INSERT INTO setup_source_suggestions(channel_id, evidence) VALUES (?, ?)
                ON CONFLICT(channel_id) DO UPDATE SET evidence = excluded.evidence
            """, (channel.id, json.dumps(candidate.evidence, ensure_ascii=False)))
            db.execute("""
                INSERT INTO initial_reactions(channel_id, threshold, sample_size) VALUES (?, ?, ?)
                ON CONFLICT(channel_id) DO UPDATE SET
                    threshold = excluded.threshold, sample_size = excluded.sample_size
            """, (channel.id, *candidate.reaction_estimate))


async def initialize(client, db, config, folder_link, *, preview=False, defer_review=False):
    """Configure routes. None reuses a saved folder; an empty link prompts again."""
    group = await resolve_group(client, config["group_id"])
    saved = _settings(db)
    folder_id = config.get("folder_id")
    if folder_id is None:
        folder_id = saved.get("folder_id")
    _check_binding(saved, group.id, folder_id)
    folder_id, channels = await _choose_folder(client, folder_id, folder_link, group.id, preview)
    if preview or folder_id is None:
        return

    existing = dict(db.execute(
        "SELECT channel_id, category FROM sources WHERE topic_id IS NOT NULL OR category = ?",
        (EXCLUDED_CATEGORY,),
    ))
    groups = defaultdict(list)
    category_overrides = config.get("category_overrides", {})
    count = sum(channel.id not in existing for channel in channels)
    if count:
        print(f"Каналов для классификации: {count}. Читаю описания и недавние посты.", flush=True)
    for channel in channels:
        if channel.id in existing:
            print(f"  {_label(channel)} — сохранено: {_visible(existing[channel.id])}")
            continue
        candidate = await _classify(client, channel, wait=True,
                                    category_override=category_overrides.get(str(channel.id)))
        groups[candidate.category if candidate.readable else None].append(candidate)
    if defer_review:
        routes = [(candidate, category) for category, candidates in groups.items() for candidate in candidates]
        _persist(db, group.id, folder_id, routes, {}, defer_review=True)
        print(f"Каналов для разбора в Telegram: {len(routes)}. Откройте /review после запуска сервиса.")
        return
    if not groups:
        if "folder_id" not in saved and _yes("Сохранить привязку к папке и форуму"):
            _persist(db, group.id, folder_id, [], {})
        return

    routes = _review_categories(groups)
    categories = list(dict.fromkeys(category for _, category in routes
                                    if category and category != EXCLUDED_CATEGORY))
    topics = await _topics(client, group) if categories else []
    plan = _topic_plan(topics, categories)
    print("Итоговые маршруты:")
    topic_titles = {topic.id: _visible(topic.title) for topic in topics}
    for candidate, category in routes:
        if category == EXCLUDED_CATEGORY:
            target = "исключён"
        elif category not in plan:
            target = "ожидает подтверждения маршрута"
        elif plan[category] is None:
            target = f"{category} → создать тему"
        else:
            topic_id = plan[category]
            target = f"{category} → {topic_titles[topic_id]} [#{topic_id}]"
        print(f"  {_label(candidate.channel)} — {target}")
    if not _yes("Применить план и сохранить маршруты"):
        return

    missing = [category for category, topic_id in plan.items() if topic_id is None]
    if missing and not (group.creator or getattr(group.admin_rights, "manage_topics", False)):
        raise ValueError("Нужно право manage_topics или ручное создание тем: " + ", ".join(missing))
    reused = {topic_id for topic_id in plan.values() if topic_id is not None}
    verified = await _verified_topics(client, group, reused)
    if verified.keys() != reused or any(
        _visible(verified[topic_id].title) != topic_titles[topic_id] for topic_id in reused
    ):
        raise ValueError("Одна из подтверждённых тем изменилась. Повторите выбор темы.")
    if plan:
        current = {channel.id: channel for channel in await folder_channels(client, folder_id, wait=True)}
        for candidate, category in routes:
            if category in plan:
                channel = current.get(candidate.channel.id)
                if channel is None or await _history(client, channel, 1) is None:
                    raise ValueError(f"Источник {_label(candidate.channel)} стал недоступен. Повторите init.")
    for title in missing:
        plan[title] = await ensure_category_topic(client, db, group, title)
    ids = set(plan.values())
    verified = await _verified_topics(client, group, ids)
    if verified.keys() != ids or any(
        _normalize(verified[plan[title]].title) != _normalize(title) for title in missing
    ) or any(
        _visible(verified[topic_id].title) != topic_titles[topic_id] for topic_id in reused
    ):
        raise ValueError("Состояние тем изменилось до сохранения маршрутов. Повторите init.")
    _persist(db, group.id, folder_id, routes, plan)
    print(f"Сохранено маршрутов: {sum(category in plan for _, category in routes)}.")
