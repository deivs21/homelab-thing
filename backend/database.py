"""Asynchronous dual-SQLite connection manager for the Dual-Hub homelab.

Two completely independent databases are managed here:

* ``./storage/notes.db``     -> Notes App module
* ``./storage/learning.db``  -> Learning Assistant module

They never share connections, tables, indexes or schemas. A failure or
migration in one module leaves the other 100 % operational.
"""

from __future__ import annotations

import os
import sqlite3
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

import aiosqlite

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
STORAGE_DIR = Path(os.getenv("STORAGE_DIR", str(BASE_DIR / "storage")))
NOTES_DB_PATH = STORAGE_DIR / "notes.db"
LEARNING_DB_PATH = STORAGE_DIR / "learning.db"

PRAGMAS = (
    "PRAGMA journal_mode = WAL;",
    "PRAGMA synchronous = NORMAL;",
    "PRAGMA busy_timeout = 5000;",
    "PRAGMA foreign_keys = ON;",
)


async def _apply_pragmas(db: aiosqlite.Connection) -> None:
    for stmt in PRAGMAS:
        await db.execute(stmt)
    await db.commit()


async def _connect(path: Path) -> aiosqlite.Connection:
    STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    db = await aiosqlite.connect(str(path))
    db.row_factory = aiosqlite.Row
    await _apply_pragmas(db)
    return db


NOTES_SCHEMA = r"""
CREATE TABLE IF NOT EXISTS notes (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    content_raw TEXT,
    content_html TEXT,
    summary TEXT,
    category TEXT,
    is_pinned INTEGER DEFAULT 0,
    is_trashed INTEGER DEFAULT 0,
    trashed_at DATETIME,
    created_at DATETIME,
    updated_at DATETIME
);

CREATE TABLE IF NOT EXISTS note_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    note_id TEXT,
    content_raw TEXT,
    saved_at DATETIME,
    FOREIGN KEY (note_id) REFERENCES notes (id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS files (
    id TEXT PRIMARY KEY,
    note_id TEXT,
    file_name TEXT,
    file_path TEXT,
    file_type TEXT,
    file_size INTEGER,
    sha256_hash TEXT,
    transcription TEXT,
    ocr_text TEXT,
    created_at DATETIME,
    FOREIGN KEY (note_id) REFERENCES notes (id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS note_embeddings (
    note_id TEXT PRIMARY KEY,
    embedding_json TEXT NOT NULL,
    FOREIGN KEY (note_id) REFERENCES notes (id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS tags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL
);

CREATE TABLE IF NOT EXISTS note_tags (
    note_id TEXT,
    tag_id INTEGER,
    PRIMARY KEY (note_id, tag_id),
    FOREIGN KEY (note_id) REFERENCES notes (id) ON DELETE CASCADE,
    FOREIGN KEY (tag_id) REFERENCES tags (id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    source_note_id TEXT,
    title TEXT NOT NULL,
    description TEXT,
    status TEXT CHECK (status IN ('todo', 'in_progress', 'done')) DEFAULT 'todo',
    priority TEXT CHECK (priority IN ('low', 'medium', 'high')) DEFAULT 'medium',
    due_date DATETIME,
    created_at DATETIME,
    FOREIGN KEY (source_note_id) REFERENCES notes (id) ON DELETE SET NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(
    title,
    content_raw,
    summary,
    content = 'notes',
    content_rowid = 'rowid'
);

CREATE TRIGGER IF NOT EXISTS notes_ai AFTER INSERT ON notes BEGIN
    INSERT INTO notes_fts (rowid, title, content_raw, summary)
    VALUES (new.rowid, new.title, new.content_raw, new.summary);
END;

CREATE TRIGGER IF NOT EXISTS notes_ad AFTER DELETE ON notes BEGIN
    INSERT INTO notes_fts (notes_fts, rowid, title, content_raw, summary)
    VALUES ('delete', old.rowid, old.title, old.content_raw, old.summary);
END;

CREATE TRIGGER IF NOT EXISTS notes_au AFTER UPDATE ON notes BEGIN
    INSERT INTO notes_fts (notes_fts, rowid, title, content_raw, summary)
    VALUES ('delete', old.rowid, old.title, old.content_raw, old.summary);
    INSERT INTO notes_fts (rowid, title, content_raw, summary)
    VALUES (new.rowid, new.title, new.content_raw, new.summary);
END;

CREATE INDEX IF NOT EXISTS idx_notes_category ON notes (category);
CREATE INDEX IF NOT EXISTS idx_notes_updated ON notes (updated_at);
CREATE INDEX IF NOT EXISTS idx_files_note ON files (note_id);
CREATE INDEX IF NOT EXISTS idx_note_tags_tag ON note_tags (tag_id);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks (status);
CREATE INDEX IF NOT EXISTS idx_tasks_due ON tasks (due_date);
"""
LEARNING_SCHEMA = r"""
CREATE TABLE IF NOT EXISTS subjects (
    id TEXT PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    color_code TEXT DEFAULT '#10B981'
);

CREATE TABLE IF NOT EXISTS topics (
    id TEXT PRIMARY KEY,
    subject_id TEXT,
    name TEXT NOT NULL,
    FOREIGN KEY (subject_id) REFERENCES subjects (id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS lessons (
    id TEXT PRIMARY KEY,
    topic_id TEXT,
    title TEXT NOT NULL,
    content_raw TEXT,
    content_html TEXT,
    summary_tldr TEXT,
    lesson_date DATE,
    FOREIGN KEY (topic_id) REFERENCES topics (id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS lesson_tags (
    lesson_id TEXT,
    tag_name TEXT,
    PRIMARY KEY (lesson_id, tag_name),
    FOREIGN KEY (lesson_id) REFERENCES lessons (id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS syllabi (
    id TEXT PRIMARY KEY,
    subject_id TEXT,
    exam_title TEXT,
    exam_date DATE,
    topics_covered_json TEXT,
    study_plan_json TEXT,
    created_at DATETIME,
    FOREIGN KEY (subject_id) REFERENCES subjects (id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS quizzes (
    id TEXT PRIMARY KEY,
    subject_id TEXT,
    topic_id TEXT,
    title TEXT,
    time_limit_seconds INTEGER,
    created_at DATETIME,
    FOREIGN KEY (subject_id) REFERENCES subjects (id) ON DELETE SET NULL,
    FOREIGN KEY (topic_id) REFERENCES topics (id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS quiz_questions (
    id TEXT PRIMARY KEY,
    quiz_id TEXT,
    question_order INTEGER,
    question_type TEXT,
    question_text TEXT,
    media_url TEXT,
    options_json TEXT,
    correct_answer TEXT,
    explanation_steps TEXT,
    hint_text TEXT,
    source_reference TEXT,
    FOREIGN KEY (quiz_id) REFERENCES quizzes (id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS quiz_attempts (
    id TEXT PRIMARY KEY,
    quiz_id TEXT,
    score_percent REAL,
    user_answers_json TEXT,
    completed_at DATETIME,
    FOREIGN KEY (quiz_id) REFERENCES quizzes (id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS flashcards (
    id TEXT PRIMARY KEY,
    subject_id TEXT,
    topic_id TEXT,
    front_text TEXT,
    back_text TEXT,
    interval_days INTEGER DEFAULT 1,
    ease_factor REAL DEFAULT 2.5,
    consecutive_correct INTEGER DEFAULT 0,
    next_review_date DATE,
    FOREIGN KEY (subject_id) REFERENCES subjects (id) ON DELETE CASCADE,
    FOREIGN KEY (topic_id) REFERENCES topics (id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS knowledge_gaps (
    id TEXT PRIMARY KEY,
    subject_id TEXT,
    concept_name TEXT,
    consecutive_mistakes INTEGER DEFAULT 0,
    consecutive_correct INTEGER DEFAULT 0,
    is_active INTEGER DEFAULT 1,
    updated_at DATETIME,
    FOREIGN KEY (subject_id) REFERENCES subjects (id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS cheat_sheets (
    id TEXT PRIMARY KEY,
    subject_id TEXT,
    topic_id TEXT,
    formula_name TEXT,
    formula_latex TEXT,
    explanation TEXT,
    is_user_verified INTEGER DEFAULT 0,
    FOREIGN KEY (subject_id) REFERENCES subjects (id) ON DELETE CASCADE,
    FOREIGN KEY (topic_id) REFERENCES topics (id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_topics_subject ON topics (subject_id);
CREATE INDEX IF NOT EXISTS idx_lessons_topic ON lessons (topic_id);
CREATE INDEX IF NOT EXISTS idx_quizzes_subject ON quizzes (subject_id);
CREATE INDEX IF NOT EXISTS idx_flashcards_next_review ON flashcards (next_review_date);
CREATE INDEX IF NOT EXISTS idx_knowledge_gaps_subject ON knowledge_gaps (subject_id);
"""


@asynccontextmanager
async def get_notes_db():
    db = await _connect(NOTES_DB_PATH)
    try:
        yield db
    finally:
        await db.close()


@asynccontextmanager
async def get_learning_db():
    db = await _connect(LEARNING_DB_PATH)
    try:
        yield db
    finally:
        await db.close()


async def init_notes_db() -> None:
    async with get_notes_db() as db:
        await db.executescript(NOTES_SCHEMA)
        await db.commit()
        logger.info("notes.db schema initialised at %s", NOTES_DB_PATH)


async def init_learning_db() -> None:
    async with get_learning_db() as db:
        await db.executescript(LEARNING_SCHEMA)
        await db.commit()
        logger.info("learning.db schema initialised at %s", LEARNING_DB_PATH)


async def init_all_databases() -> None:
    errors = []
    try:
        await init_notes_db()
    except Exception as exc:
        logger.exception("Failed to initialise notes.db")
        errors.append(("notes", exc))
    try:
        await init_learning_db()
    except Exception as exc:
        logger.exception("Failed to initialise learning.db")
        errors.append(("learning", exc))
    if errors:
        raise RuntimeError(f"Database initialisation errors: {errors}")


__all__ = [
    "NOTES_DB_PATH",
    "LEARNING_DB_PATH",
    "STORAGE_DIR",
    "get_notes_db",
    "get_learning_db",
    "init_notes_db",
    "init_learning_db",
    "init_all_databases",
]

