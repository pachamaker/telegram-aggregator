"""CLI for a single Telegram Aggregator instance."""
import argparse
import asyncio
import fcntl
import getpass
import json
import logging
import os
import sqlite3
import sys
import tempfile
import time
from contextlib import contextmanager, suppress
from pathlib import Path

from telethon import TelegramClient, errors, types, utils

from .admin import run_admin
from .review import Review
from .rules import fingerprint
from .runtime import (RATE_LIMITS, confirm, fetch_posts, legacy_keys, refresh_sources, run,
                      source_link, topic_is_open, verify_destination)
from .setup import _read as read_with_backoff
from .setup import ensure_category_topic, initialize, list_open_topics, resolve_group
from .store import migrate_v1, open_db, seed_sources, setting, validate_config

LOG = logging.getLogger(__name__)


@contextmanager
def locked_state(path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (path / "v2.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("Каталог состояния занят. Сначала остановите сервис.") from None
        yield


def credentials(path):
    local = {}
    if path.exists():
        if path.is_symlink() or path.stat().st_mode & 0o077:
            raise ValueError("credentials.json должен быть обычным файлом с правами 600.")
        local = json.loads(path.read_text(encoding="utf-8"))
    api_id = os.getenv("TELEGRAM_API_ID") or local.get("api_id")
    api_hash = os.getenv("TELEGRAM_API_HASH") or local.get("api_hash")
    if sys.stdin.isatty() and not (api_id and api_hash):
        api_id = api_id or input("Telegram api_id: ").strip()
        api_hash = api_hash or getpass.getpass("Telegram api_hash: ").strip()
    if not (api_id and str(api_id).isdigit() and int(api_id) > 0 and api_hash):
        raise ValueError("Укажите TELEGRAM_API_ID и TELEGRAM_API_HASH или выполните auth.")
    return int(api_id), api_hash


def save_credentials(path, api_id, api_hash):
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as file:
        temporary = Path(file.name)
        try:
            json.dump({"api_id": api_id, "api_hash": api_hash}, file)
            file.close()
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


async def reconcile(client, db, group, queue_id, ids, retry, max_media_bytes):
    row = db.execute("SELECT * FROM queue WHERE id=? AND status='sending'", (queue_id,)).fetchone()
    if not row:
        raise ValueError("Запись с неподтверждённым исходом не найдена.")
    known_ids = json.loads(row["dest_ids"] or "[]")
    if retry:
        if known_ids and any(await client.get_messages(group, ids=known_ids)):
            raise ValueError("Сообщения с ID из попытки ещё присутствуют в форуме.")
        with db:
            db.execute("""
                UPDATE queue SET status='pending', dest_ids=NULL, random_ids=NULL, keys=NULL,
                    legacy_keys=NULL, cancel_retry=0, retry_at=0, due=? WHERE id=?
            """, (int(time.time()), queue_id))
        return
    if known_ids and known_ids != ids:
        raise ValueError("ID отличаются от подтверждения, сохранённого в очереди.")
    if not ids or not row["topic_id"]:
        raise ValueError("Для сверки нужны ID опубликованных сообщений и целевой темы.")
    messages = await verify_destination(client, group, row["topic_id"], ids)
    if not known_ids:
        source = db.execute("SELECT * FROM sources WHERE channel_id=?", (row["channel_id"],)).fetchone()
        original_ids = json.loads(row["post_ids"])
        if len(messages) == 1 and messages[0].out and messages[0].raw_text == source_link(source, original_ids[0]):
            pass
        else:
            expected = [(row["channel_id"], post_id) for post_id in original_ids]
            actual = [(getattr(getattr(m.fwd_from, "from_id", None), "channel_id", None),
                       getattr(m.fwd_from, "channel_post", None)) for m in messages if m.out]
            if actual != expected:
                raise ValueError("Не удалось связать пересылку с исходным сообщением.")
    keys = json.loads((row["legacy_keys"] if row["policy"] == "legacy" else row["keys"]) or "[]")
    if row["keys"] is None:
        source = db.execute("SELECT * FROM sources WHERE channel_id=?", (row["channel_id"],)).fetchone()
        try:
            peer = await client.get_entity(types.PeerChannel(row["channel_id"]))
            posts = await fetch_posts(client, peer, row)
        except RATE_LIMITS:
            raise
        except (ValueError, errors.RPCError):
            posts = []
        if posts:
            budget = 0 if getattr(peer, "noforwards", False) else max_media_bytes
            canonical = await fingerprint(client, posts, row["channel_id"], max_media_bytes=budget)
            legacy = setting(db, "legacy_policy", {"news_topics": []})
            keys = legacy_keys(posts, source, legacy) if row["policy"] == "legacy" else canonical
            with db:
                db.execute("UPDATE queue SET keys=?, legacy_keys=? WHERE id=?",
                           (json.dumps(canonical), json.dumps(keys) if row["policy"] == "legacy" else None,
                            row["id"]))
            row = db.execute("SELECT * FROM queue WHERE id=?", (row["id"],)).fetchone()
    if not keys:
        prefix = ""
        if row["policy"] == "legacy":
            category = db.execute("SELECT category FROM sources WHERE channel_id=?",
                                  (row["channel_id"],)).fetchone()[0]
            policy = setting(db, "legacy_policy", {"news_topics": []})
            prefix = "news:" if category in policy["news_topics"] else "memes:"
        keys = [f"{prefix}origin:{row['channel_id']}:{post_id}" for post_id in json.loads(row["post_ids"])]
    confirm(db, row, row["topic_id"], ids, keys, int(time.time()))


async def connected(args, db, config):
    credential_path = args.state_dir / "credentials.json"
    api_id, api_hash = credentials(credential_path)
    client = TelegramClient(
        str(args.state_dir / "telegram"), api_id, api_hash, receive_updates=False,
        request_retries=5 if args.command == "auth" else 0, raise_last_call_error=True,
        flood_sleep_threshold=0,
    )
    await client.connect()
    try:
        if not await client.is_user_authorized():
            if args.command != "auth" or not sys.stdin.isatty():
                raise ValueError("Сначала выполните auth в интерактивном терминале.")
            await client.start()
        if (await client.get_me()).bot:
            raise ValueError("Для MTProto требуется пользовательская сессия.")
        if args.command == "auth":
            save_credentials(credential_path, api_id, api_hash)
            print("Пользовательская сессия и ключи сохранены в закрытом каталоге.")
            return
        if args.command == "forums":
            async for dialog in client.iter_dialogs():
                entity = dialog.entity
                if getattr(entity, "megagroup", False) and getattr(entity, "forum", False):
                    print(json.dumps({"id": entity.id, "title": entity.title}, ensure_ascii=False))
            return
        if args.command == "init":
            await initialize(client, db, config, "" if args.invite else None,
                             preview=args.preview, defer_review=args.defer_review)
            return
        group = await resolve_group(client, config["group_id"])
        saved_group = setting(db, "group_id")
        if saved_group is not None and saved_group != group.id:
            raise ValueError("Форум в конфиге отличается от форума, записанного в базе.")
        if args.command == "admin-info":
            print("Открытые темы (admin_topic_id)")
            for topic_id, title in await list_open_topics(client, group):
                print(f"  {topic_id} — {title}")
            print("Администраторы (operator_ids)")
            async for user in client.iter_participants(group, filter=types.ChannelParticipantsAdmins()):
                if not user.bot:
                    print(f"  {user.id} — {utils.get_display_name(user)}")
            return
        if args.command == "reconcile":
            await reconcile(client, db, group, args.queue_id, args.message_ids,
                            args.retry_confirmed_absent, config.get("max_media_bytes", 20 * 1024 * 1024))
            print("Решение по отправке сохранено.")
            return
        if args.command == "run":
            if not (setting(db, "folder_id") or config.get("folder_id")):
                raise ValueError("Запустите init или задайте folder_id для уже подключённой папки.")
        seed_sources(db, config, group.id)
        if args.command == "check":
            peers = await refresh_sources(client, db, discover=False)
            rows = db.execute("SELECT * FROM sources WHERE enabled=1 AND topic_id IS NOT NULL").fetchall()
            if not rows:
                raise ValueError("Нет активных источников. Завершите init.")
            topics = {row["topic_id"] for row in rows}
            missing = [topic for topic in sorted(topics) if not await topic_is_open(client, group, topic)]
            if missing:
                raise ValueError("Недоступные темы: " + ", ".join(map(str, missing)))
            unavailable = []
            for row in rows:
                peer = peers.get(row["channel_id"])
                if peer is None:
                    unavailable.append(row["channel_id"])
                    continue
                try:
                    await read_with_backoff(client.get_messages, peer, limit=1)
                except RATE_LIMITS:
                    raise
                except errors.RPCError:
                    unavailable.append(row["channel_id"])
            if unavailable:
                raise ValueError("Недоступные источники: " + ", ".join(map(str, unavailable)))
            print(f"Проверено источников: {len(rows)}, тем: {len(topics)}.")
            return
        token = os.getenv("AGGREGATOR_BOT_TOKEN")
        bot_task = None
        if token:
            admin = {**config, "group_id": group.id}
            if not (admin.get("operator_ids") and admin.get("admin_topic_id")):
                LOG.warning("Админ-бот отключён: задайте operator_ids и admin_topic_id.")
            else:
                async def validate_topic(topic_id):
                    try:
                        return await topic_is_open(client, group, topic_id)
                    except (errors.RPCError, OSError, asyncio.TimeoutError):
                        LOG.warning("Telegram временно не подтвердил тему %s.", topic_id)
                        return False

                async def review_topics():
                    return await list_open_topics(client, group)

                async def review_create(title):
                    topic_id = await ensure_category_topic(client, db, group, title)
                    if topic_id == admin["admin_topic_id"]:
                        raise ValueError("Тема настроек не используется для публикаций.")
                    return topic_id

                async def review_source(channel_id):
                    peer = await client.get_entity(types.PeerChannel(channel_id))
                    if not isinstance(peer, types.Channel) or not peer.broadcast or peer.left:
                        return False
                    await client.get_messages(peer, limit=1)
                    return True

                dictionary = json.loads(Path(__file__).with_name("categories.json").read_text(encoding="utf-8"))
                categories = (*dictionary["categories"], *config.get("category_overrides", {}).values())
                review = Review(db, categories, review_topics, review_create, review_source)
                bot_task = asyncio.create_task(run_admin(db, admin, token, validate_topic, review))
        try:
            await run(client, db, config, group)
        finally:
            if bot_task:
                bot_task.cancel()
                with suppress(asyncio.CancelledError):
                    await bot_task
    finally:
        await client.disconnect()


def parser():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--config", type=Path, default=Path("config.json"))
    cli.add_argument("--state-dir", type=Path, default=Path("state"))
    commands = cli.add_subparsers(dest="command", required=True)
    commands.add_parser("auth", help="Войти под пользовательским аккаунтом Telegram")
    commands.add_parser("forums", help="Показать доступные целевые форумы и их ID")
    commands.add_parser("admin-info", help="Показать ID тем и администраторов целевого форума")
    init = commands.add_parser("init", help="Импортировать общую папку и настроить маршруты")
    init_mode = init.add_mutually_exclusive_group()
    init_mode.add_argument("--preview", action="store_true", help="Показать состав приглашения")
    init_mode.add_argument("--defer-review", action="store_true", help="Перенести выбор тем в Telegram")
    init.add_argument("--invite", action="store_true", help="Запросить ссылку на настроенную папку повторно")
    commands.add_parser("check", help="Проверить подключение, источники и темы")
    commands.add_parser("run", help="Запустить агрегацию")
    migration = commands.add_parser("migrate-v1", help="Перенести состояние из остановленного MVP")
    migration.add_argument("--legacy-db", type=Path, required=True)
    migration.add_argument("--legacy-config", type=Path, required=True)
    migration.add_argument("--preview", action="store_true")
    commands.add_parser("pending", help="Показать отправки, требующие ручной сверки")
    policy = commands.add_parser("policy", help="Выбрать политику источника для новых постов")
    policy.add_argument("channel_id", type=int)
    policy.add_argument("mode", choices=("legacy", "shadow", "v2"))
    resolve = commands.add_parser("reconcile", help="Разрешить результат отправки после проверки")
    resolve.add_argument("queue_id", type=int)
    outcome = resolve.add_mutually_exclusive_group(required=True)
    outcome.add_argument("--message-ids", nargs="+", type=int)
    outcome.add_argument("--retry-confirmed-absent", action="store_true")
    return cli


def main():
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = parser().parse_args()
    try:
        with locked_state(args.state_dir):
            db = open_db(args.state_dir / "aggregator-v2.sqlite3")
            try:
                if args.command == "migrate-v1":
                    config = json.loads(args.legacy_config.read_text(encoding="utf-8"))
                    print(json.dumps(migrate_v1(db, args.legacy_db, config, preview=args.preview),
                                     ensure_ascii=False, indent=2))
                elif args.command == "pending":
                    for row in db.execute("""
                        SELECT id, channel_id, post_ids, topic_id, dest_ids FROM queue WHERE status='sending'
                    """):
                        print(json.dumps(dict(row), ensure_ascii=False))
                elif args.command == "policy":
                    with db:
                        result = db.execute("UPDATE sources SET policy=? WHERE channel_id=?",
                                            (args.mode, args.channel_id))
                        if not result.rowcount:
                            raise ValueError("Источник не найден.")
                    print("Политика сохранена для новых записей очереди.")
                else:
                    config = {} if args.command in ("auth", "forums") else validate_config(
                        json.loads(args.config.read_text(encoding="utf-8")))
                    asyncio.run(connected(args, db, config))
            finally:
                db.close()
    except KeyboardInterrupt:
        return 0
    except RATE_LIMITS as exc:
        print(f"Telegram просит повторить через {exc.seconds} с.", file=sys.stderr)
        return 1
    except errors.RPCError as exc:
        print(f"Ошибка Telegram: {type(exc).__name__}", file=sys.stderr)
        return 1
    except (ValueError, OSError, KeyError, sqlite3.Error) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
