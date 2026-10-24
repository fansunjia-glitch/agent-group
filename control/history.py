"""Durable, paginated topic conversations, independent of the live event window."""

import json
from pathlib import Path
import sqlite3
from threading import RLock


def _event_topic_ids(event):
    """Read protocol fields only, never search human/model message text."""
    if not isinstance(event, dict):
        return set()
    identifiers = set()

    def collect(value):
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                return
        rows = value if isinstance(value, list) else [value]
        for row in rows:
            if isinstance(row, dict) and isinstance(row.get("topic_id"), str):
                identifiers.add(row["topic_id"])

    collect(event)
    if event.get("type") in ("tool_started", "tool_finished"):
        for field in ("args", "arguments", "result"):
            collect(event.get(field))
        # create_topic returns its identifier directly, unlike discover_topics'
        # JSON array. Exact equality is intentional; free-text matches are not.
        if (event.get("tool_name", event.get("name")) == "create_topic"
                and event.get("type") == "tool_finished"
                and isinstance(event.get("result"), str)):
            identifiers.add(event["result"])
    return identifiers


def event_references_topic(event, topic_id):
    """Shared cascade predicate for the archive, live deque and JSONL logs."""
    return topic_id in _event_topic_ids(event)


class TopicHistory:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = RLock()
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.path.chmod(0o600)
        self.db.row_factory = sqlite3.Row
        self.db.create_function("casefold", 1, lambda value: str(value or "").casefold(), deterministic=True)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS events (
                event_id TEXT PRIMARY KEY, ts TEXT NOT NULL,
                topic_id TEXT, kind TEXT NOT NULL, raw TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS events_topic ON events(topic_id, ts);
            CREATE TABLE IF NOT EXISTS topics (
                topic_id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS messages (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT NOT NULL UNIQUE,
                topic_id TEXT NOT NULL, ts TEXT NOT NULL, author TEXT NOT NULL,
                profile_id TEXT, content TEXT NOT NULL, truncated INTEGER NOT NULL DEFAULT 0,
                notice_id TEXT
            );
            CREATE INDEX IF NOT EXISTS messages_topic_seq ON messages(topic_id, seq);
            CREATE TABLE IF NOT EXISTS imports (path TEXT PRIMARY KEY, fingerprint TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS deleted_profiles (profile_id TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS deleted_topics (topic_id TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS history_metadata (
                singleton INTEGER PRIMARY KEY CHECK(singleton=1), revision INTEGER NOT NULL
            );
            INSERT OR IGNORE INTO history_metadata VALUES(1,0);
        """)
        # Older archives stored the author only inside raw JSON. Backfill an
        # indexed column once so profile cleanup includes non-message events.
        columns = {row["name"] for row in self.db.execute("PRAGMA table_info(events)")}
        with self.db:
            # sqlite3's legacy mode does not begin transactions for DDL. Keep
            # the column addition and full backfill atomic across interruptions.
            self.db.execute("BEGIN")
            if "profile_id" not in columns:
                self.db.execute("ALTER TABLE events ADD COLUMN profile_id TEXT")
                for row in self.db.execute("SELECT event_id,raw FROM events").fetchall():
                    try:
                        profile_id = json.loads(row["raw"]).get("profile_id")
                    except (ValueError, AttributeError):
                        continue
                    if isinstance(profile_id, str):
                        self.db.execute("UPDATE events SET profile_id=? WHERE event_id=?",
                                        (profile_id, row["event_id"]))
            self.db.execute("CREATE INDEX IF NOT EXISTS events_profile ON events(profile_id)")
            self.db.execute("CREATE INDEX IF NOT EXISTS messages_profile ON messages(profile_id)")

    def close(self):
        with self.lock:
            self.db.close()

    def revision(self):
        """Durable cache generation; only destructive history changes bump it."""
        with self.lock:
            return self.db.execute("SELECT revision FROM history_metadata WHERE singleton=1").fetchone()[0]

    def _deleted_profile(self, identifier):
        return isinstance(identifier, str) and self.db.execute(
            "SELECT 1 FROM deleted_profiles WHERE profile_id=?", (identifier,)).fetchone() is not None

    def _deleted_topic(self, identifier):
        return isinstance(identifier, str) and self.db.execute(
            "SELECT 1 FROM deleted_topics WHERE topic_id=?", (identifier,)).fetchone() is not None

    @staticmethod
    def _validate_deletion_id(identifier):
        if not isinstance(identifier, str) or not identifier.strip():
            raise ValueError("Deletion ID must be a non-empty string")

    def delete_profile(self, identifier):
        """Purge this profile's records without altering shared conversations.

        The durable tombstone also rejects late worker events and old JSONL
        imports. Other participants' messages and topic metadata remain intact.
        """
        self._validate_deletion_id(identifier)
        with self.lock, self.db:
            created = self.db.execute("INSERT OR IGNORE INTO deleted_profiles VALUES(?)", (identifier,)).rowcount
            messages = self.db.execute("DELETE FROM messages WHERE profile_id=?", (identifier,)).rowcount
            events = self.db.execute("DELETE FROM events WHERE profile_id=?", (identifier,)).rowcount
            if created or messages or events:
                self.db.execute("UPDATE history_metadata SET revision=revision+1 WHERE singleton=1")
            revision = self.revision()
        return {"deleted_profile": identifier, "messages_deleted": messages,
                "events_deleted": events, "topics_deleted": 0, "history_revision": revision}

    def delete_topic(self, identifier):
        """Purge a topic's archive and prevent stale discovery from reviving it."""
        self._validate_deletion_id(identifier)
        with self.lock, self.db:
            created = self.db.execute("INSERT OR IGNORE INTO deleted_topics VALUES(?)", (identifier,)).rowcount
            messages = self.db.execute("DELETE FROM messages WHERE topic_id=?", (identifier,)).rowcount
            events = self.db.execute("DELETE FROM events WHERE topic_id=?", (identifier,)).rowcount
            # Tool trace records can carry their topic only inside structured
            # arguments/results, so the indexed top-level predicate is not enough.
            nested_ids = []
            for row in self.db.execute("SELECT event_id,raw FROM events"):
                try:
                    event = json.loads(row["raw"])
                except ValueError:
                    continue
                if event_references_topic(event, identifier):
                    nested_ids.append((row["event_id"],))
            self.db.executemany("DELETE FROM events WHERE event_id=?", nested_ids)
            events += len(nested_ids)
            topics = self.db.execute("DELETE FROM topics WHERE topic_id=?", (identifier,)).rowcount
            if created or messages or events or topics:
                self.db.execute("UPDATE history_metadata SET revision=revision+1 WHERE singleton=1")
            revision = self.revision()
        return {"deleted_topic": identifier, "messages_deleted": messages,
                "events_deleted": events, "topics_deleted": topics, "history_revision": revision}

    def _remember_topic(self, topic_id, name=None, description=None):
        if not isinstance(topic_id, str) or not topic_id or self._deleted_topic(topic_id):
            return
        name = name if isinstance(name, str) and name.strip() else ""
        description = description if isinstance(description, str) else ""
        self.db.execute("""INSERT INTO topics(topic_id,name,description) VALUES(?,?,?)
            ON CONFLICT(topic_id) DO UPDATE SET
                name=CASE WHEN ?<>'' THEN ? ELSE topics.name END,
                description=CASE WHEN ?<>'' THEN ? ELSE topics.description END""",
            (topic_id, name or f"历史话题 {topic_id[:8]}", description, name, name, description, description))

    def remember_topics(self, topics):
        with self.lock, self.db:
            for topic in topics:
                self._remember_topic(topic.get("topic_id"), topic.get("name"), topic.get("description"))

    def _append_event(self, event):
        if not isinstance(event, dict) or not isinstance(event.get("id"), str):
            return
        topic_id = event.get("topic_id")
        if self._deleted_profile(event.get("profile_id")) or any(
                self._deleted_topic(identifier) for identifier in _event_topic_ids(event)):
            return
        kind = event.get("type", "event")
        raw = json.dumps(event, ensure_ascii=False)
        cursor = self.db.execute("""INSERT OR IGNORE INTO events
            (event_id,ts,topic_id,kind,raw,profile_id) VALUES(?,?,?,?,?,?)""",
            (event["id"], str(event.get("ts", "")), topic_id, kind, raw, event.get("profile_id")))
        if topic_id:
            self._remember_topic(topic_id, event.get("topic_name"), event.get("description"))
        if kind != "message_sent" or not isinstance(topic_id, str) or not isinstance(event.get("payload"), str):
            return
        key = "sent-" + event["id"]
        if cursor.rowcount:
            self.db.execute("""INSERT OR IGNORE INTO messages
                (key,topic_id,ts,author,profile_id,content,truncated,notice_id) VALUES(?,?,?,?,?,?,?,NULL)""",
                (key, topic_id, str(event.get("ts", "")), event.get("name") or "未知成员",
                 event.get("profile_id"), event["payload"], int(bool(event.get("truncated")))))
        elif not event.get("truncated"):
            # A recovery pass may upgrade a legacy truncated body, but never
            # replace an already complete message or allocate a new cursor.
            changed = self.db.execute("UPDATE messages SET content=?, truncated=0 WHERE key=? AND truncated=1",
                                      (event["payload"], key)).rowcount
            if changed:
                self.db.execute("UPDATE events SET raw=? WHERE event_id=?", (raw, event["id"]))

    def append_event(self, event):
        with self.lock, self.db:
            self._append_event(event)

    def _add_notice(self, notice):
        if not isinstance(notice, dict) or not notice.get("id") or not notice.get("topic_id"):
            return
        if self._deleted_topic(notice["topic_id"]):
            return
        self._remember_topic(notice["topic_id"])
        self.db.execute("""INSERT OR IGNORE INTO messages
            (key,topic_id,ts,author,profile_id,content,truncated,notice_id) VALUES(?,?,?,'管理员',NULL,?,0,?)""",
            ("notice-" + notice["id"], notice["topic_id"], str(notice.get("created_at", "")),
             str(notice.get("content", "")), notice["id"]))

    def add_notice(self, notice):
        with self.lock, self.db:
            self._add_notice(notice)

    def import_legacy(self, root, notices=()):
        """Idempotently import every surviving JSONL rotation and saved notice.

        Only changed files are parsed. SQLite is the canonical history after
        migration; rotating the compatibility JSONL can never evict messages.
        """
        entries, fingerprints = [], []
        with self.lock:
            previous = dict(self.db.execute("SELECT path,fingerprint FROM imports"))
        paths = sorted(Path(root).glob("events*.jsonl"), key=lambda p: (p.stat().st_mtime_ns, p.name))
        for path in paths:
            stat = path.stat()
            fingerprint = f"{stat.st_ino}:{stat.st_size}:{stat.st_mtime_ns}"
            if previous.get(path.name) == fingerprint:
                continue
            with path.open(encoding="utf-8", errors="replace") as stream:
                for line in stream:
                    try:
                        event = json.loads(line)
                        if isinstance(event, dict) and isinstance(event.get("id"), str):
                            entries.append((str(event.get("ts", "")), "event", event))
                    except (ValueError, UnicodeError):
                        continue
            fingerprints.append((path.name, fingerprint))
        entries.extend((str(n.get("created_at", "")), "notice", n) for n in notices if isinstance(n, dict))
        entries.sort(key=lambda item: item[0])
        with self.lock, self.db:
            for _, kind, value in entries:
                self._append_event(value) if kind == "event" else self._add_notice(value)
            self.db.executemany("INSERT OR REPLACE INTO imports VALUES(?,?)", fingerprints)

    def truncated_sent_events(self):
        with self.lock:
            return [json.loads(row[0]) for row in self.db.execute("""SELECT e.raw FROM events e
                JOIN messages m ON m.key='sent-'||e.event_id WHERE m.truncated=1 ORDER BY m.seq""")]

    def recent_events(self, limit=400):
        with self.lock:
            rows = self.db.execute("SELECT raw FROM events ORDER BY ts DESC, rowid DESC LIMIT ?", (limit,)).fetchall()
        return [json.loads(row[0]) for row in reversed(rows)]

    @staticmethod
    def _message(row):
        if row is None:
            return None
        result = dict(row)
        result.pop("topic_id", None)
        result["truncated"] = bool(result["truncated"])
        return result

    def topic(self, identifier):
        with self.lock:
            row = self.db.execute("SELECT * FROM topics WHERE topic_id=?", (identifier,)).fetchone()
            return dict(row) if row else None

    def topics(self):
        with self.lock:
            topics = []
            for row in self.db.execute("""SELECT t.*,count(m.seq) AS message_count,max(m.seq) AS last_seq
                FROM topics t LEFT JOIN messages m ON m.topic_id=t.topic_id GROUP BY t.topic_id"""):
                topic = dict(row)
                last_seq = topic.pop("last_seq")
                topic["last_message"] = self._message(self.db.execute("SELECT seq,key,topic_id,ts,author,profile_id,substr(content,1,200) AS content,truncated,notice_id FROM messages WHERE seq=?", (last_seq,)).fetchone())
                topic["members"] = [dict(m) for m in self.db.execute("""SELECT profile_id,author AS name FROM messages WHERE seq IN (
                    SELECT max(seq) FROM messages WHERE topic_id=? AND profile_id IS NOT NULL GROUP BY profile_id)""", (topic["topic_id"],))]
                topic["is_historical"] = True
                topics.append(topic)
            return topics

    def message_page(self, identifier, *, before=None, after=None, limit=50, query=""):
        if before is not None and after is not None:
            raise ValueError("before and after cannot be combined")
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError("limit must be an integer from 1 to 200")
        for cursor in (before, after):
            if cursor is not None and (type(cursor) is not int or not 0 <= cursor <= 9_223_372_036_854_775_807):
                raise ValueError("History cursor must be a non-negative integer")
        if not isinstance(query, str) or len(query) > 500:
            raise ValueError("Search text must be at most 500 characters")
        where, values = ["topic_id=?"], [identifier]
        if query:
            where.append("(instr(casefold(content),?)>0 OR instr(casefold(author),?)>0)")
            values.extend([query.casefold(), query.casefold()])
        matching = " AND ".join(where)
        with self.lock:
            total = self.db.execute(f"SELECT count(*) FROM messages WHERE {matching}", values).fetchone()[0]
            if before is not None:
                where.append("seq<?"); values.append(before)
            if after is not None:
                where.append("seq>?"); values.append(after)
            order = "ASC" if after is not None else "DESC"
            rows = self.db.execute(f"SELECT * FROM messages WHERE {' AND '.join(where)} ORDER BY seq {order} LIMIT ?",
                                   [*values, limit + 1]).fetchall()
            has_more = len(rows) > limit
            rows = rows[:limit]
            if after is None:
                rows.reverse()
            revision = self.revision()
        messages = [self._message(row) for row in rows]
        return {"messages": messages, "total": total, "has_more": has_more,
                "next_before": messages[0]["seq"] if messages else None,
                "latest_cursor": messages[-1]["seq"] if messages else (after or 0),
                "history_revision": revision}
