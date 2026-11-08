"""Durable state for one forum."""
import json
import re
import sqlite3
import unicodedata
from pathlib import Path


def open_db(path):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.executescript("""
        PRAGMA foreign_keys = ON;
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY, value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sources (
            channel_id INTEGER PRIMARY KEY, title TEXT NOT NULL, username TEXT,
            category TEXT NOT NULL, topic_id INTEGER, enabled INTEGER NOT NULL DEFAULT 1,
            reaction_mode TEXT NOT NULL DEFAULT 'auto', threshold INTEGER,
            reaction_hours INTEGER NOT NULL DEFAULT 2, policy TEXT NOT NULL DEFAULT 'v2'
        );
        CREATE TABLE IF NOT EXISTS cursors (
            channel_id INTEGER PRIMARY KEY REFERENCES sources, last_id INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS queue (
            id INTEGER PRIMARY KEY, channel_id INTEGER NOT NULL REFERENCES sources,
            post_ids TEXT NOT NULL, published_at INTEGER NOT NULL, due INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending', policy TEXT NOT NULL,
            lane TEXT NOT NULL DEFAULT 'live', topic_id INTEGER, dest_ids TEXT, random_ids TEXT,
            keys TEXT, legacy_keys TEXT, cancel_retry INTEGER NOT NULL DEFAULT 0,
            retry_at INTEGER NOT NULL DEFAULT 0,
            UNIQUE(channel_id, post_ids, lane)
        );
        CREATE INDEX IF NOT EXISTS due_queue ON queue(status, due);
        CREATE TABLE IF NOT EXISTS seen (
            lane TEXT NOT NULL, key TEXT NOT NULL, published_at INTEGER NOT NULL,
            topic_id INTEGER, dest_ids TEXT, PRIMARY KEY(lane, key)
        );
        CREATE TABLE IF NOT EXISTS measurements (
            channel_id INTEGER NOT NULL REFERENCES sources, post_ids TEXT NOT NULL,
            due INTEGER NOT NULL, hours INTEGER NOT NULL, count INTEGER, measured_at INTEGER,
            PRIMARY KEY(channel_id, post_ids, hours)
        );
        CREATE INDEX IF NOT EXISTS measurement_due ON measurements(measured_at, due);
        CREATE TABLE IF NOT EXISTS baselines (
            channel_id INTEGER NOT NULL REFERENCES sources, hours INTEGER NOT NULL,
            threshold INTEGER NOT NULL, sample_size INTEGER NOT NULL, calculated_at INTEGER NOT NULL,
            PRIMARY KEY(channel_id, hours)
        );
        CREATE TABLE IF NOT EXISTS decisions (
            id INTEGER PRIMARY KEY, channel_id INTEGER NOT NULL REFERENCES sources,
            post_ids TEXT NOT NULL, reason TEXT NOT NULL, details TEXT NOT NULL,
            decided_at INTEGER NOT NULL, shadow INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS legacy_audit (
            kind TEXT NOT NULL, record TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS setup_source_suggestions (
            channel_id INTEGER PRIMARY KEY REFERENCES sources, evidence TEXT NOT NULL
        );
    """)
    return db


def setting(db, key, default=None):
    row = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return json.loads(row["value"]) if row else default


def save_setting(db, key, value):
    db.execute(
        "INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)", (key, json.dumps(value))
    )


def identifier(value):
    return type(value) is int and value > 0


def validate_config(config):
    if not isinstance(config, dict):
        raise ValueError("Конфигурация должна быть объектом JSON.")
    group = config.get("group_id")
    if not identifier(group) and not (isinstance(group, str) and group.startswith("@") and len(group) > 1):
        raise ValueError("group_id должен быть положительным ID или @username форума.")
    for key in ("folder_id", "admin_topic_id"):
        if key in config and config[key] is not None and not identifier(config[key]):
            raise ValueError(f"{key} должен быть положительным числом.")
    operators = config.get("operator_ids", [])
    if not isinstance(operators, list) or any(not identifier(value) for value in operators):
        raise ValueError("operator_ids должен быть списком положительных ID.")
    if type(config.get("max_media_bytes", 20 * 1024 * 1024)) is not int or config.get("max_media_bytes", 0) < 0:
        raise ValueError("max_media_bytes должен быть неотрицательным целым числом.")
    overrides = config.get("category_overrides", {})
    if not isinstance(overrides, dict):
        raise ValueError("category_overrides должен быть объектом с ID каналов и названиями категорий.")
    for key, title in overrides.items():
        if (not isinstance(key, str) or not re.fullmatch(r"[1-9][0-9]*", key)
                or not isinstance(title, str) or not title or len(title.encode("utf-8")) > 128
                or title != " ".join(title.split())
                or any(unicodedata.category(char).startswith("C") for char in title)
                or title.casefold() in ("нужно уточнить", "исключён")):
            raise ValueError("В category_overrides нужны положительные ID и допустимые названия категорий.")
    sources = config.get("sources", [])
    if not isinstance(sources, list):
        raise ValueError("sources должен быть списком.")
    seen = set()
    for source in sources:
        if not isinstance(source, dict) or not identifier(source.get("channel_id")):
            raise ValueError("Каждый источник должен содержать положительный channel_id.")
        if source["channel_id"] in seen:
            raise ValueError("Повторяющийся channel_id в sources.")
        seen.add(source["channel_id"])
        if not identifier(source.get("topic_id")) or source["topic_id"] == 1:
            raise ValueError("Каждый источник должен содержать topic_id именованной темы.")
        if not isinstance(source.get("category"), str) or not source["category"].strip():
            raise ValueError("Каждый источник должен содержать category.")
        if not isinstance(source.get("title", ""), str):
            raise ValueError("title должен быть строкой.")
        username = source.get("username")
        if username is not None and not isinstance(username, str):
            raise ValueError("username должен быть строкой.")
        if source.get("reaction_mode", "auto") not in ("auto", "off", "manual"):
            raise ValueError("Неверный reaction_mode.")
        if source.get("reaction_mode") == "manual" and (
            type(source.get("threshold")) is not int or source["threshold"] < 0
        ):
            raise ValueError("Для ручного режима требуется threshold ≥ 0.")
        if "reaction_hours" in source and not identifier(source["reaction_hours"]):
            raise ValueError("reaction_hours должен быть положительным целым числом.")
    return config


def seed_sources(db, config, group_id):
    """Explicit bootstrap config is applied only once."""
    old_group = setting(db, "group_id")
    if old_group is not None and old_group != group_id:
        raise ValueError("База уже принадлежит другому форуму.")
    with db:
        if old_group is None:
            save_setting(db, "group_id", group_id)
        if setting(db, "folder_id") is None and config.get("folder_id"):
            save_setting(db, "folder_id", config["folder_id"])
        if setting(db, "seed_applied", False):
            return
        db.executemany("""
            INSERT OR IGNORE INTO sources
            (channel_id, title, username, category, topic_id, reaction_mode, threshold, reaction_hours)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, [
            (s["channel_id"], s.get("title", str(s["channel_id"])), s.get("username"),
             s["category"], s["topic_id"], s.get("reaction_mode", "auto"), s.get("threshold"),
             s.get("reaction_hours", 12 if s["category"] in ("humor", "memes", "Юмор") else 2))
            for s in config.get("sources", [])
        ])
        save_setting(db, "seed_applied", True)


def migrate_v1(db, old_path, config, *, preview=False):
    """Import the actual v1 SQLite schema with the instance's v1 config."""
    old_path = Path(old_path).resolve(strict=True)
    if setting(db, "migration_v1", False):
        raise ValueError("Эта база уже прошла перенос v1.")
    if db.execute("SELECT 1 FROM sources LIMIT 1").fetchone() or setting(db, "group_id") is not None:
        raise ValueError("Перенос v1 требует пустого экземпляра v2.")
    mapped = {}
    destination = config["destination"]
    for source in config["sources"]:
        handle = source["channel"].casefold()
        if not identifier(source.get("id")) or handle in mapped:
            raise ValueError("Для переноса нужны однозначные ID всех источников в конфиге v1.")
        mapped[handle] = source
    if len({s["id"] for s in mapped.values()}) != len(mapped):
        raise ValueError("Повторяющийся ID источника в конфиге v1.")
    with sqlite3.connect(old_path.as_uri() + "?mode=ro", uri=True) as old:
        old.row_factory = sqlite3.Row
        old.execute("BEGIN")
        tables = {row[0] for row in old.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"cursors", "pending", "seen"} <= tables:
            raise ValueError("Файл не содержит ожидаемую схему v1.")
        cursors = list(old.execute("SELECT channel, last_id FROM cursors"))
        pending = list(old.execute("SELECT channel, post_ids, due, status FROM pending"))
        seen = list(old.execute("SELECT key, published_at FROM seen"))
        archived = [(table, json.dumps(dict(row), ensure_ascii=False))
                    for table in ("review_cards", "skip_audit", "skip_audit_window")
                    if table in tables for row in old.execute(f"SELECT * FROM {table}")]
        legacy_hours = int(config["memes"]["check_after_hours"])
        if "meme_queue_delay" in tables:
            tracked = old.execute("SELECT hours FROM meme_queue_delay WHERE id = 1").fetchone()
            if tracked:
                legacy_hours = tracked[0]
        unknown = sorted({r["channel"] for r in cursors + pending if r["channel"].casefold() not in mapped})
        if unknown:
            raise ValueError("Нет ID для источников v1: " + ", ".join(unknown))
        if len({r["channel"].casefold() for r in cursors}) != len(cursors):
            raise ValueError("Две позиции v1 указывают на один источник.")
        if any(r["status"] not in ("pending", "sending") for r in pending):
            raise ValueError("Обнаружен неизвестный статус очереди v1.")
        counts = {"sources": len(mapped), "cursors": len(cursors), "queued": len(pending),
                  "unresolved": sum(r["status"] == "sending" for r in pending),
                  "fingerprints": len(seen), "audit": len(archived)}
        if preview:
            return counts
        with db:
            for s in mapped.values():
                category = s["topic"]
                db.execute("""
                    INSERT INTO sources(channel_id, title, username, category, topic_id, reaction_hours, policy)
                    VALUES (?, ?, ?, ?, ?, ?, 'legacy')
                """, (s["id"], s["channel"], s["channel"].lstrip("@") if s["channel"].startswith("@") else None,
                      category, destination["topic_ids"][category], legacy_hours if category == "memes" else 2))
            db.executemany("INSERT INTO cursors VALUES (?, ?)",
                           [(mapped[r["channel"].casefold()]["id"], r["last_id"]) for r in cursors])
            for r in pending:
                s = mapped[r["channel"].casefold()]
                ids = json.dumps(json.loads(r["post_ids"]))
                age = 3600 * legacy_hours if s["topic"] == "memes" else 0
                db.execute("""
                    INSERT INTO queue(channel_id, post_ids, published_at, due, status, policy, topic_id)
                    VALUES (?, ?, ?, ?, ?, 'legacy', ?)
                """, (s["id"], ids, r["due"] - age, r["due"], r["status"],
                      destination["topic_ids"][s["topic"]] if r["status"] == "sending" else None))
            db.executemany("INSERT INTO seen(lane, key, published_at) VALUES ('legacy', ?, ?)",
                           [(r["key"], r["published_at"]) for r in seen])
            db.executemany("INSERT INTO legacy_audit VALUES (?, ?)", archived)
            save_setting(db, "group_id", destination["group_id"])
            if config.get("source_folder_id") is not None:
                save_setting(db, "folder_id", config["source_folder_id"])
            save_setting(db, "legacy_policy", {
                "hours": legacy_hours,
                "threshold": config["memes"].get("min_reactions_per_1000_subscribers",
                                                config["memes"].get("min_positive_reactions_per_1000_subscribers", 7)),
                "positive": {str(s["id"]): s["positive_reactions"] for s in mapped.values()
                             if "min_reactions_per_1000_subscribers" not in config["memes"]
                             and "positive_reactions" in s},
                "news_topics": config["news_deduplication"]["topics"],
                "dedup_hours": config["news_deduplication"]["lookback_hours"],
            })
            save_setting(db, "migration_v1", True)
            save_setting(db, "seed_applied", True)
        return counts
