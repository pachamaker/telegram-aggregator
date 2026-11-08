"""Collect complete Telegram posts, evaluate lanes and confirm deliveries."""
import asyncio
import hashlib
import json
import logging
import math
import re
import secrets
import time
from collections import defaultdict
from statistics import median

from telethon import errors, functions, types, utils

from .rules import advertising_reason, fingerprint, reaction_availability, reaction_count
from .setup import folder_channels, suggest_channel
from .store import setting

LOG = logging.getLogger(__name__)
POLL_INTERVAL = 600
FOLDER_INTERVAL = 24 * 3600
DEDUP_WINDOW = 72 * 3600
BASELINE_WINDOW = 30 * 86400
BASELINE_MINIMUM = 20
RATE_LIMITS = (errors.FloodWaitError, errors.FloodPremiumWaitError, errors.SlowModeWaitError)


def source_link(source, message_id):
    username = source["username"]
    path = username.lstrip("@") if username else f"c/{source['channel_id']}"
    return f"https://t.me/{path}/{message_id}"


def enqueue(db, source, posts, last_id, legacy):
    """Commit cursor and all work discovered in this batch together."""
    batches = defaultdict(list)
    for post in posts:
        key = ("album", post.grouped_id) if post.grouped_id else ("post", post.id)
        batches[key].append(post)
    with db:
        for batch in batches.values():
            ids = json.dumps([post.id for post in batch])
            published_at = int(batch[0].date.timestamp())
            policy = source["policy"]
            if policy in ("legacy", "shadow"):
                delay = legacy["hours"] * 3600 if source["category"] in ("memes", "humor", "Юмор") else 0
                lanes = [("live", "legacy", published_at + delay)]
                if policy == "shadow":
                    lanes.append(("shadow", "v2", published_at))
            else:
                lanes = [("live", "v2", published_at)]
            db.executemany("""
                INSERT OR IGNORE INTO queue(channel_id, post_ids, published_at, due, lane, policy)
                VALUES (?, ?, ?, ?, ?, ?)
            """, [(source["channel_id"], ids, published_at, due, lane, rule)
                  for lane, rule, due in lanes])
            if policy != "legacy" and source["reaction_mode"] != "off":
                hours = source["reaction_hours"]
                db.execute("""
                    INSERT OR IGNORE INTO measurements(channel_id, post_ids, due, hours)
                    VALUES (?, ?, ?, ?)
                """, (source["channel_id"], ids, published_at + hours * 3600, hours))
        db.execute("INSERT OR REPLACE INTO cursors VALUES (?, ?)", (source["channel_id"], last_id))


async def collect(client, db, source, peer, legacy):
    cursor = db.execute("SELECT last_id FROM cursors WHERE channel_id = ?",
                        (source["channel_id"],)).fetchone()
    if cursor is None:
        newest = await client.get_messages(peer, limit=1)
        current = read_source(db, source["channel_id"])
        if current["enabled"]:
            enqueue(db, current, [], newest[0].id if newest else 0, legacy)
        return
    posts = []
    last_id = cursor["last_id"]
    cutoff = time.time() - 30
    async for post in client.iter_messages(peer, min_id=last_id, reverse=True):
        if post.date.timestamp() > cutoff:
            group_id = getattr(post, "grouped_id", None)
            if group_id and posts and posts[-1].grouped_id == group_id:
                while posts and posts[-1].grouped_id == group_id:
                    posts.pop()
                last_id = posts[-1].id if posts else cursor["last_id"]
            break
        last_id = post.id
        if isinstance(post, types.Message):
            posts.append(post)
    current = read_source(db, source["channel_id"])
    cursor_now = db.execute("SELECT last_id FROM cursors WHERE channel_id = ?",
                            (source["channel_id"],)).fetchone()
    if current["enabled"] and cursor_now == cursor:
        enqueue(db, current, posts, last_id, legacy)


def read_source(db, channel_id):
    return db.execute("SELECT * FROM sources WHERE channel_id = ?", (channel_id,)).fetchone()


def recompute_baseline(db, source, now):
    channel_id, hours = source["channel_id"], source["reaction_hours"]
    cached = db.execute("""
        SELECT * FROM baselines WHERE channel_id = ? AND hours = ?
    """, (channel_id, hours)).fetchone()
    if cached and cached["calculated_at"] >= now - 86400:
        return cached["threshold"]
    rows = db.execute("""
        SELECT count FROM measurements
        WHERE channel_id = ? AND hours = ? AND count IS NOT NULL AND measured_at >= ?
        ORDER BY measured_at DESC, due DESC LIMIT 30
    """, (channel_id, hours, now - BASELINE_WINDOW)).fetchall()
    with db:
        if len(rows) < BASELINE_MINIMUM:
            db.execute("DELETE FROM baselines WHERE channel_id = ? AND hours = ?", (channel_id, hours))
            return None
        threshold = math.ceil(median(row["count"] for row in rows))
        db.execute("INSERT OR REPLACE INTO baselines VALUES (?, ?, ?, ?, ?)",
                   (channel_id, hours, threshold, len(rows), now))
    return threshold


def legacy_keys(posts, source, policy):
    scope = "news" if source["category"] in policy["news_topics"] else "memes"
    keys = set()
    for post in posts:
        keys.add(f"{scope}:origin:{source['channel_id']}:{post.id}")
        origin = post.fwd_from
        original_channel = getattr(getattr(origin, "from_id", None), "channel_id", None)
        original_id = getattr(origin, "channel_post", None)
        if original_channel and original_id:
            keys.add(f"{scope}:origin:{original_channel}:{original_id}")
        if scope == "memes":
            for attr in ("photo", "document"):
                media_id = getattr(getattr(post, attr, None), "id", None)
                if media_id:
                    keys.add(f"{scope}:{attr}:{media_id}")
    body = re.sub(r"\s+", " ", " ".join(post.raw_text or "" for post in posts)).strip().casefold()
    if scope == "news" and body:
        keys.add("news:text:" + hashlib.sha256(body.encode()).hexdigest())
    return sorted(keys)


def duplicate(db, lane, keys, since):
    for key in keys:
        entry = db.execute("""
            SELECT * FROM seen WHERE lane = ? AND key = ? AND published_at >= ?
        """, (lane, key, since)).fetchone()
        if entry:
            return entry
        if lane == "live" and key.startswith("origin:"):
            entry = db.execute("""
                SELECT * FROM seen WHERE lane = 'legacy'
                AND key IN (?, ?) AND published_at >= ? LIMIT 1
            """, (f"news:{key}", f"memes:{key}", since)).fetchone()
            if entry:
                return entry
    return None


def log_decision(db, row, reason, details, now):
    db.execute("""
        INSERT INTO decisions(channel_id, post_ids, reason, details, decided_at, shadow)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (row["channel_id"], row["post_ids"], reason, json.dumps(details, ensure_ascii=False),
          now, row["lane"] == "shadow"))


def skip(db, row, reason, details, now):
    with db:
        updated = db.execute("UPDATE queue SET status='skipped' WHERE id=? AND status='pending'",
                             (row["id"],))
        if updated.rowcount:
            log_decision(db, row, reason, details, now)


def confirm(db, row, topic_id, destination_ids, keys, now):
    lane = "legacy" if row["policy"] == "legacy" else row["lane"]
    ids_json = json.dumps(destination_ids)
    with db:
        status = "pending" if row["lane"] == "shadow" else "sending"
        updated = db.execute("""
            UPDATE queue SET status='sent', topic_id=?, dest_ids=? WHERE id=? AND status=?
        """, (topic_id, ids_json, row["id"], status))
        if not updated.rowcount:
            return
        db.executemany("INSERT OR REPLACE INTO seen VALUES (?, ?, ?, ?, ?)",
                       [(lane, key, now, topic_id, ids_json) for key in keys])
        if lane == "legacy":
            # Live v2 compares exact v2 keys from successful legacy deliveries too.
            canonical = json.loads(row["keys"] or "[]")
            db.executemany("INSERT OR REPLACE INTO seen VALUES ('live', ?, ?, ?, ?)",
                           [(key, now, topic_id, ids_json) for key in canonical])
        elif lane == "live":
            db.executemany("INSERT OR REPLACE INTO seen VALUES ('v2_only', ?, ?, ?, ?)",
                           [(key, now, topic_id, ids_json) for key in keys])
        log_decision(db, row, "published", {"topic_id": topic_id, "message_ids": destination_ids}, now)


async def topic_is_open(client, group, topic_id):
    if not isinstance(topic_id, int) or topic_id <= 1:
        return False
    response = await client(functions.messages.GetForumTopicsByIDRequest(peer=group, topics=[topic_id]))
    group_id = utils.get_peer_id(group, add_mark=False)
    return any(isinstance(topic, types.ForumTopic) and topic.id == topic_id
               and getattr(topic.peer, "channel_id", None) == group_id
               and not topic.closed and not topic.hidden
               for topic in response.topics)


async def verify_destination(client, group, topic_id, ids):
    result = await client.get_messages(group, ids=ids)
    if len(result) != len(ids):
        raise ValueError("Telegram вернул неполное подтверждение.")
    for message, expected_id in zip(result, ids):
        reply = getattr(message, "reply_to", None)
        actual = getattr(reply, "reply_to_top_id", None) or getattr(reply, "reply_to_msg_id", None)
        if not message or message.id != expected_id or actual != topic_id:
            raise ValueError("Пересылка не подтверждена в целевой теме.")
    return result


async def deliver(client, db, group, peer, source, row, posts, keys, canonical, now):
    topic_id = source["topic_id"]
    random_ids = [secrets.randbits(63) for _ in posts]
    with db:
        claimed = db.execute("""
            UPDATE queue SET status='sending', topic_id=?, random_ids=?, keys=?, legacy_keys=?,
                dest_ids=NULL, cancel_retry=0
            WHERE id=? AND status='pending' AND EXISTS(
                SELECT 1 FROM sources WHERE channel_id=? AND enabled=1 AND topic_id=?
            )
        """, (topic_id, json.dumps(random_ids), json.dumps(canonical),
              json.dumps(keys) if row["policy"] == "legacy" else None, row["id"],
              source["channel_id"], topic_id))
    if not claimed.rowcount:
        return
    try:
        try:
            result = await client(functions.messages.ForwardMessagesRequest(
                from_peer=peer, id=[post.id for post in posts], random_id=random_ids,
                to_peer=group, top_msg_id=topic_id, silent=True,
            ))
        except errors.ChatForwardsRestrictedError:
            random_ids = [secrets.randbits(63)]
            with db:
                db.execute("UPDATE queue SET random_ids=? WHERE id=?", (json.dumps(random_ids), row["id"]))
            result = await client(functions.messages.SendMessageRequest(
                peer=group, reply_to=types.InputReplyToMessage(reply_to_msg_id=topic_id),
                message=source_link(source, posts[0].id), random_id=random_ids[0], no_webpage=True, silent=True,
            ))
    except RATE_LIMITS as exc:
        with db:
            at = int(time.time())
            cancelled = db.execute("SELECT cancel_retry FROM queue WHERE id=?", (row["id"],)).fetchone()[0]
            if cancelled:
                db.execute("UPDATE queue SET status='skipped' WHERE id=?", (row["id"],))
                log_decision(db, row, "paused_by_operator", {}, at)
            else:
                db.execute("UPDATE queue SET status='pending', retry_at=? WHERE id=?",
                           (at + exc.seconds, row["id"]))
        raise
    except Exception as exc:
        LOG.warning("Очередь %s требует сверки: %s", row["id"], type(exc).__name__)
        return
    try:
        if isinstance(result, types.UpdateShortSentMessage) and len(random_ids) == 1:
            ids = [result.id]
        else:
            by_random = {u.random_id: u.id for u in getattr(result, "updates", [])
                         if isinstance(u, types.UpdateMessageID)}
            ids = [by_random[value] for value in random_ids]
        with db:
            db.execute("UPDATE queue SET dest_ids=? WHERE id=?", (json.dumps(ids), row["id"]))
        await verify_destination(client, group, topic_id, ids)
    except Exception as exc:
        LOG.warning("Очередь %s требует сверки: %s", row["id"], type(exc).__name__)
        return
    claimed_row = db.execute("SELECT * FROM queue WHERE id=?", (row["id"],)).fetchone()
    confirm(db, claimed_row, topic_id, ids, keys, int(time.time()))
    LOG.info("Публикация %s → тема %s, сообщения %s", row["id"], topic_id, ids)


async def fetch_posts(client, peer, row):
    ids = json.loads(row["post_ids"])
    result = await client.get_messages(peer, ids=ids)
    posts = [p for p in result if isinstance(p, types.Message)]
    if [p.id for p in posts] != ids:
        return []
    return posts


class Pass:
    def __init__(self, client, db, group, peers, config, legacy):
        self.client, self.db, self.group, self.peers = client, db, group, peers
        self.config, self.legacy = config, legacy
        self.full = {}
        self.open_topics = {}
        self.post_cache = {}
        self.key_cache = {}
        self.unmatched_legacy_attempt = db.execute("""
            SELECT id FROM queue WHERE status='sending' AND policy='legacy' AND keys IS NULL LIMIT 1
        """).fetchone()

    async def channel_full(self, channel_id, peer):
        if channel_id not in self.full:
            response = await self.client(functions.channels.GetFullChannelRequest(channel=peer))
            self.full[channel_id] = response.full_chat
        return self.full[channel_id]

    async def posts(self, row, peer):
        key = (row["channel_id"], row["post_ids"])
        if key not in self.post_cache:
            self.post_cache[key] = await fetch_posts(self.client, peer, row)
        return self.post_cache[key]

    async def canonical(self, row, posts, peer, *, download=True):
        max_bytes = (self.config.get("max_media_bytes", 20 * 1024 * 1024)
                     if download and not getattr(peer, "noforwards", False) else 0)
        key = (row["channel_id"], row["post_ids"], max_bytes)
        if key not in self.key_cache:
            self.key_cache[key] = await fingerprint(self.client, posts, row["channel_id"], max_media_bytes=max_bytes)
        return self.key_cache[key]

    async def measure(self, row, source, peer, now):
        count = None
        if row["hours"] == source["reaction_hours"] and row["due"] + 3600 >= now:
            full = await self.channel_full(row["channel_id"], peer)
            if reaction_availability(full) is True:
                posts = await self.posts(row, peer)
                if posts and advertising_reason(posts) is None:
                    keys = await self.canonical(row, posts, peer)
                    lane = "shadow" if source["policy"] == "shadow" else "live"
                    match = duplicate(self.db, lane, keys, now - DEDUP_WINDOW)
                    own = self.db.execute("""
                        SELECT dest_ids, topic_id FROM queue WHERE channel_id=? AND post_ids=? AND lane=?
                    """, (row["channel_id"], row["post_ids"], lane)).fetchone()
                    if not match or (own and own["dest_ids"] is not None
                                     and own["dest_ids"] == match["dest_ids"] and own["topic_id"] == match["topic_id"]):
                        count = reaction_count(posts)
        with self.db:
            self.db.execute("""
                UPDATE measurements SET count=?, measured_at=? WHERE channel_id=? AND post_ids=? AND hours=?
            """, (count, now, row["channel_id"], row["post_ids"], row["hours"]))

    async def evaluate(self, row, source, peer, now):
        posts = await self.posts(row, peer)
        if not posts:
            skip(self.db, row, "unavailable", {}, now)
            return
        canonical = []
        if row["policy"] == "legacy":
            keys = legacy_keys(posts, source, self.legacy)
            match = duplicate(self.db, "legacy", keys, now - self.legacy["dedup_hours"] * 3600)
        else:
            if reason := advertising_reason(posts):
                skip(self.db, row, reason, {}, now)
                return
            keys = await self.canonical(row, posts, peer, download=False)
            match = duplicate(self.db, row["lane"], keys, now - DEDUP_WINDOW)
            if not match:
                keys = await self.canonical(row, posts, peer)
                match = duplicate(self.db, row["lane"], keys, now - DEDUP_WINDOW)
            canonical = keys
        if match:
            skip(self.db, row, "duplicate",
                 {"topic_id": match["topic_id"], "message_ids": json.loads(match["dest_ids"] or "[]")}, now)
            return

        if row["policy"] == "legacy" and source["category"] in ("memes", "humor", "Юмор"):
            full = await self.channel_full(row["channel_id"], peer)
            if not full.participants_count:
                return
            allowed = self.legacy.get("positive", {}).get(str(row["channel_id"]))
            count = reaction_count(posts) if allowed is None else max(
                sum(r.count for r in getattr(p.reactions, "results", [])
                    if getattr(r.reaction, "emoticon", None) in allowed) for p in posts
            )
            if count * 1000 < self.legacy["threshold"] * full.participants_count:
                skip(self.db, row, "low_reactions", {"count": count, "per_thousand": self.legacy["threshold"]}, now)
                return
        elif row["policy"] == "v2" and source["reaction_mode"] != "off":
            threshold = (source["threshold"] if source["reaction_mode"] == "manual"
                         else recompute_baseline(self.db, source, now))
            try:
                full = await self.channel_full(row["channel_id"], peer)
            except RATE_LIMITS:
                raise
            except errors.RPCError:
                full = None
            availability = reaction_availability(full)
            if availability is None:
                LOG.info("Источник %s: состояние реакций неизвестно", row["channel_id"])
            if availability is True and threshold is not None:
                due = row["published_at"] + source["reaction_hours"] * 3600
                if now < due:
                    with self.db:
                        self.db.execute("UPDATE queue SET due=? WHERE id=? AND status='pending'", (due, row["id"]))
                    return
                count = reaction_count(posts)
                if count < threshold:
                    skip(self.db, row, "low_reactions", {"count": count, "threshold": threshold}, now)
                    return
        if row["lane"] == "shadow":
            confirm(self.db, row, source["topic_id"], [], keys, now)
            return
        if self.unmatched_legacy_attempt:
            return
        topic = source["topic_id"]
        if topic not in self.open_topics:
            self.open_topics[topic] = await topic_is_open(self.client, self.group, topic)
        if not self.open_topics[topic]:
            LOG.warning("Тема %s недоступна. Запись %s сохранена.", topic, row["id"])
            return
        if row["policy"] == "legacy":
            canonical = await self.canonical(row, posts, peer)
            if match := duplicate(self.db, "v2_only", canonical, now - DEDUP_WINDOW):
                skip(self.db, row, "duplicate",
                     {"topic_id": match["topic_id"], "message_ids": json.loads(match["dest_ids"] or "[]")}, now)
                return
        if any(self.db.execute("""
            SELECT 1 FROM queue, json_each(queue.keys)
            WHERE status='sending' AND json_each.value=? LIMIT 1
        """, (key,)).fetchone() for key in canonical):
            LOG.warning("Запись %s ожидает сверки другой отправки с тем же содержимым.", row["id"])
            return
        await deliver(self.client, self.db, self.group, peer, source, row, posts, keys, canonical, now)

    async def run(self):
        now = int(time.time())
        if self.unmatched_legacy_attempt:
            LOG.warning("Новые отправки ждут сверки перенесённой записи %s.",
                        self.unmatched_legacy_attempt["id"])
        with self.db:
            self.db.execute("DELETE FROM seen WHERE published_at < ?",
                            (now - max(DEDUP_WINDOW, self.legacy["dedup_hours"] * 3600),))
            self.db.execute("DELETE FROM decisions WHERE decided_at < ?", (now - BASELINE_WINDOW,))
            self.db.execute("DELETE FROM measurements WHERE due < ?", (now - BASELINE_WINDOW,))
            self.db.execute("""
                DELETE FROM queue WHERE status IN ('sent', 'skipped') AND published_at < ?
            """, (now - BASELINE_WINDOW,))
        pending = self.db.execute("""
            SELECT * FROM queue WHERE status='pending' AND due<=? AND retry_at<=? ORDER BY due, id
        """, (now, now)).fetchall()
        for row in pending:
            peer = self.peers.get(row["channel_id"])
            source = read_source(self.db, row["channel_id"])
            if peer and source["enabled"] and source["topic_id"]:
                try:
                    await self.evaluate(row, source, peer, now)
                except RATE_LIMITS:
                    raise
                except (OSError, asyncio.TimeoutError, errors.RPCError) as exc:
                    LOG.warning("Запись %s отложена: %s", row["id"], type(exc).__name__)
        measurements = self.db.execute("""
            SELECT * FROM measurements WHERE measured_at IS NULL AND due<=? ORDER BY due
        """, (now,)).fetchall()
        for row in measurements:
            peer = self.peers.get(row["channel_id"])
            source = read_source(self.db, row["channel_id"])
            if peer and source["enabled"] and source["reaction_mode"] != "off":
                try:
                    await self.measure(row, source, peer, now)
                except RATE_LIMITS:
                    raise
                except (OSError, asyncio.TimeoutError, errors.RPCError) as exc:
                    LOG.warning("Замер для источника %s отложен: %s", row["channel_id"], type(exc).__name__)


async def refresh_sources(client, db, *, discover=True):
    folder_id = setting(db, "folder_id")
    if folder_id is None:
        raise ValueError("Настройте общую папку через init или folder_id.")
    peers = {}
    for peer in await folder_channels(client, folder_id):
        peers[peer.id] = peer
        if not discover:
            continue
        source = read_source(db, peer.id)
        if source:
            with db:
                db.execute("UPDATE sources SET title=?, username=? WHERE channel_id=?",
                           (peer.title, peer.username, peer.id))
            continue
        try:
            suggestion, reasons = await suggest_channel(client, peer)
        except RATE_LIMITS:
            raise
        except (OSError, asyncio.TimeoutError, errors.RPCError) as exc:
            LOG.warning("Классификация %s отложена: %s", peer.id, type(exc).__name__)
            continue
        category = suggestion or "Нужно уточнить"
        with db:
            db.execute("""
                INSERT OR IGNORE INTO sources(channel_id, title, username, category, enabled, reaction_hours)
                VALUES (?, ?, ?, ?, 0, ?)
            """, (peer.id, peer.title, peer.username, category, 12 if category == "Юмор" else 2))
            db.execute("INSERT OR IGNORE INTO setup_source_suggestions VALUES (?, ?)",
                       (peer.id, json.dumps(reasons, ensure_ascii=False)))
        LOG.info("Нужен маршрут для %s. Категория: %s. Признаки: %s", peer.id, category, ", ".join(reasons))
    return peers


async def run(client, db, config, group):
    legacy = setting(db, "legacy_policy", {"hours": 12, "threshold": 7,
                     "news_topics": ["news", "it", "ai", "product", "Новости", "Технологии", "Бизнес"],
                     "dedup_hours": 72})
    peers = {}
    refresh_at = 0.0
    while True:
        delay = POLL_INTERVAL
        try:
            if time.monotonic() >= refresh_at:
                try:
                    peers = await refresh_sources(client, db)
                    refresh_at = time.monotonic() + FOLDER_INTERVAL
                except ValueError as exc:
                    LOG.warning("Не удалось сверить папку: %s", exc)
                    # A known missing folder cannot safely authorize delivery.
                    peers = {}
            for source in db.execute("SELECT * FROM sources WHERE enabled=1 AND topic_id IS NOT NULL").fetchall():
                peer = peers.get(source["channel_id"])
                if peer:
                    try:
                        await collect(client, db, source, peer, legacy)
                    except RATE_LIMITS:
                        raise
                    except (OSError, asyncio.TimeoutError, errors.RPCError) as exc:
                        LOG.warning("Источник %s отложен: %s", source["channel_id"], type(exc).__name__)
            await Pass(client, db, group, peers, config, legacy).run()
        except RATE_LIMITS as exc:
            delay = max(delay, exc.seconds)
        except (OSError, asyncio.TimeoutError, errors.RPCError) as exc:
            LOG.warning("Опрос отложен: %s", type(exc).__name__)
        LOG.info("Проход завершён. Источников в папке: %s. Пауза: %s с.", len(peers), delay)
        await asyncio.sleep(delay)
