"""Notes App service: storage, ingestion, CRUD, search, versioning, trash."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiofiles
from fastapi import HTTPException

from backend.database import get_notes_db
from backend.shared.gemini_service import get_gemini_service, sanitize_emojis

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent.parent
STORAGE_DIR = Path(os.getenv("STORAGE_DIR", str(BASE_DIR / "storage")))
UPLOADS_DIR = STORAGE_DIR / "uploads"
TEMP_DIR = UPLOADS_DIR / "temp"
TRASH_DIR = STORAGE_DIR / "trash"
SETTINGS_FILE = STORAGE_DIR / "notes_settings.json"

FORBIDDEN_EXTENSIONS = {".zip", ".tar", ".tgz", ".tar.gz", ".tar.bz2"}
ALLOWED_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp",
    ".mp3", ".wav", ".m4a", ".ogg", ".flac", ".aac",
    ".pdf", ".txt", ".md", ".markdown", ".csv",
    ".py", ".js", ".ts", ".java", ".c", ".cpp", ".h", ".go", ".rs",
    ".json", ".xml", ".yaml", ".yml", ".html", ".css", ".sh",
}

MAX_CHUNK_SIZE = 20 * 1024 * 1024


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id() -> str:
    return str(uuid.uuid4())


def cosine_similarity(a: List[float], b: List[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def load_settings() -> Dict[str, Any]:
    try:
        if SETTINGS_FILE.exists():
            return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {"task_description_style": "detailed"}


def save_settings(data: Dict[str, Any]) -> None:
    STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(data), encoding="utf-8")


def ensure_dirs() -> None:
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    TRASH_DIR.mkdir(parents=True, exist_ok=True)
    STORAGE_DIR.mkdir(parents=True, exist_ok=True)


ensure_dirs()
class NotesService:
    def __init__(self):
        self.gemini = get_gemini_service()

    async def handle_chunk(
        self,
        upload_id: str,
        chunk_index: int,
        total_chunks: int,
        file_name: str,
        chunk: bytes,
    ) -> Dict[str, Any]:
        ensure_dirs()
        ext = Path(file_name).suffix.lower()
        if ext in FORBIDDEN_EXTENSIONS:
            raise HTTPException(status_code=400, detail=f"File type {ext} is not allowed.")
        if ext and ext not in ALLOWED_EXTENSIONS:
            raise HTTPException(status_code=400, detail=f"Unsupported file type {ext}.")

        temp_dir = TEMP_DIR / upload_id
        temp_dir.mkdir(parents=True, exist_ok=True)
        chunk_path = temp_dir / f"chunk_{chunk_index:05d}"
        async with aiofiles.open(chunk_path, "wb") as f:
            await f.write(chunk)

        manifest = {"file_name": file_name, "total_chunks": total_chunks}
        async with aiofiles.open(temp_dir / "manifest.json", "w") as f:
            await f.write(json.dumps(manifest))

        received = [p for p in temp_dir.iterdir() if p.name.startswith("chunk_")]
        if len(received) < total_chunks:
            return {"status": "chunk_received", "upload_id": upload_id, "received": len(received), "total": total_chunks}

        assembled_path = temp_dir / f"assembled_{file_name}"
        sha = hashlib.sha256()
        async with aiofiles.open(assembled_path, "wb") as out:
            for i in range(total_chunks):
                cp = temp_dir / f"chunk_{i:05d}"
                async with aiofiles.open(cp, "rb") as inp:
                    while True:
                        data = await inp.read(65536)
                        if not data:
                            break
                        await out.write(data)
                        sha.update(data)

        file_hash = sha.hexdigest()

        for i in range(total_chunks):
            cp = temp_dir / f"chunk_{i:05d}"
            try:
                cp.unlink(missing_ok=True)
            except Exception:
                pass

        async with get_notes_db() as db:
            cur = await db.execute("SELECT id, note_id, file_name FROM files WHERE sha256_hash = ?", (file_hash,))
            row = await cur.fetchone()

        if row:
            pending_info = {
                "upload_id": upload_id,
                "file_name": file_name,
                "file_hash": file_hash,
                "assembled_path": str(assembled_path),
                "duplicate_file_id": row["id"],
                "duplicate_note_id": row["note_id"],
            }
            async with aiofiles.open(temp_dir / "pending.json", "w") as f:
                await f.write(json.dumps(pending_info))
            return {
                "status": "duplicate",
                "upload_id": upload_id,
                "sha256": file_hash,
                "file_name": file_name,
                "existing": {
                    "file_id": row["id"],
                    "note_id": row["note_id"],
                    "file_name": row["file_name"],
                },
                "options": ["overwrite", "skip", "cancel"],
            }

        result = await self.process_upload(upload_id, assembled_path, file_name, file_hash, temp_dir)
        return result

    async def resolve_upload(self, upload_id: str, action: str) -> Dict[str, Any]:
        temp_dir = TEMP_DIR / upload_id
        pending_path = temp_dir / "pending.json"
        if not pending_path.exists():
            raise HTTPException(status_code=404, detail="No pending upload found.")
        async with aiofiles.open(pending_path, "r") as f:
            pending = json.loads(await f.read())
        assembled_path = Path(pending["assembled_path"])
        file_name = pending["file_name"]
        file_hash = pending["file_hash"]

        if action in ("cancel", "skip"):
            try:
                assembled_path.unlink(missing_ok=True)
            except Exception:
                pass
            shutil.rmtree(temp_dir, ignore_errors=True)
            return {"status": "skipped" if action == "skip" else "cancelled", "upload_id": upload_id}

        if action == "overwrite":
            existing_file_id = pending["duplicate_file_id"]
            async with get_notes_db() as db:
                cur = await db.execute("SELECT file_path FROM files WHERE id = ?", (existing_file_id,))
                r = await cur.fetchone()
                if r and r["file_path"]:
                    old_path = Path(r["file_path"])
                    if old_path.exists():
                        trash_dest = TRASH_DIR / old_path.name
                        try:
                            shutil.move(str(old_path), str(trash_dest))
                        except Exception:
                            pass
                await db.execute("DELETE FROM files WHERE id = ?", (existing_file_id,))
                await db.commit()
            result = await self.process_upload(upload_id, assembled_path, file_name, file_hash, temp_dir)
            return result

        raise HTTPException(status_code=400, detail="Invalid action.")

    async def process_upload(
        self, upload_id: str, assembled_path: Path, file_name: str, file_hash: str, temp_dir: Path
    ) -> Dict[str, Any]:
        ensure_dirs()
        ext = Path(file_name).suffix.lower()
        final_path = UPLOADS_DIR / f"{new_id()}_{file_name}"
        shutil.move(str(assembled_path), str(final_path))
        shutil.rmtree(temp_dir, ignore_errors=True)

        file_size = final_path.stat().st_size
        file_type = ext.lstrip(".").lower()

        text = await self._extract_text(final_path, file_type, file_name)

        settings = load_settings()
        style = settings.get("task_description_style", "detailed")
        extraction = await self._ai_extract(text, file_name, style)

        title = sanitize_emojis(extraction.get("title")) or Path(file_name).stem
        summary = sanitize_emojis(extraction.get("summary")) or ""
        tags = extraction.get("tags") or []
        tasks = extraction.get("tasks") or []
        content_raw = sanitize_emojis(extraction.get("content")) or text
        category = sanitize_emojis(extraction.get("category"))

        note_id = new_id()
        file_id = new_id()

        async with get_notes_db() as db:
            await db.execute(
                """INSERT INTO notes (id, title, content_raw, content_html, summary, category, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (note_id, title, content_raw, "", summary, category, now_iso(), now_iso()),
            )
            for tag in tags:
                tag = str(tag).lower().strip()
                tag = sanitize_emojis(tag) or ""
                if not tag:
                    continue
                await db.execute("INSERT OR IGNORE INTO tags (name) VALUES (?)", (tag,))
                cur = await db.execute("SELECT id FROM tags WHERE name = ?", (tag,))
                r = await cur.fetchone()
                if r:
                    await db.execute("INSERT OR IGNORE INTO note_tags (note_id, tag_id) VALUES (?,?)", (note_id, r["id"]))
            for t in tasks:
                task_id = new_id()
                await db.execute(
                    """INSERT INTO tasks (id, source_note_id, title, description, status, priority, due_date, created_at)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (
                        task_id,
                        note_id,
                        sanitize_emojis(t.get("title", "Task")) or "Task",
                        sanitize_emojis(t.get("description", "")) or "",
                        "todo",
                        t.get("priority", "medium") if t.get("priority") in ("low", "medium", "high") else "medium",
                        t.get("due_date"),
                        now_iso(),
                    ),
                )
            await db.execute(
                """INSERT INTO files (id, note_id, file_name, file_path, file_type, file_size, sha256_hash, transcription, ocr_text, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (file_id, note_id, file_name, str(final_path), file_type, file_size, file_hash,
                 sanitize_emojis(extraction.get("transcription", "")) or "",
                 sanitize_emojis(extraction.get("ocr_text", "")) or "", now_iso()),
            )
            await db.commit()

        try:
            emb = await self.gemini.embed_text(content_raw[:8000])
            vec = emb.get("embedding", [])
            async with get_notes_db() as db:
                await db.execute(
                    "INSERT OR REPLACE INTO note_embeddings (note_id, embedding_json) VALUES (?,?)",
                    (note_id, json.dumps(vec)),
                )
                await db.commit()
        except Exception as e:
            logger.warning("Embedding failed: %s", e)

        chapters = extraction.get("chapters")
        if isinstance(chapters, list) and len(chapters) > 1:
            for ch in chapters[1:]:
                ch_title = sanitize_emojis(ch.get("title", title)) or title
                ch_content = sanitize_emojis(ch.get("content", "")) or ""
                ch_id = new_id()
                async with get_notes_db() as db:
                    await db.execute(
                        """INSERT INTO notes (id, title, content_raw, content_html, summary, category, created_at, updated_at)
                           VALUES (?,?,?,?,?,?,?,?)""",
                        (ch_id, ch_title, ch_content, "", sanitize_emojis(ch.get("summary", "")) or "", category, now_iso(), now_iso()),
                    )
                    await db.commit()
                try:
                    emb = await self.gemini.embed_text(ch_content[:8000])
                    async with get_notes_db() as db:
                        await db.execute(
                            "INSERT OR REPLACE INTO note_embeddings (note_id, embedding_json) VALUES (?,?)",
                            (ch_id, json.dumps(emb.get("embedding", []))),
                        )
                        await db.commit()
                except Exception:
                    pass

        return {
            "status": "processed",
            "upload_id": upload_id,
            "note_id": note_id,
            "title": title,
            "summary": summary,
            "tags": tags,
            "tasks_created": len(tasks),
            "file_id": file_id,
            "file_path": str(final_path),
        }

    async def _extract_text(self, path: Path, file_type: str, file_name: str) -> str:
        image_exts = {"png", "jpg", "jpeg", "gif", "webp", "bmp"}
        audio_exts = {"mp3", "wav", "m4a", "ogg", "flac", "aac"}
        if file_type in image_exts:
            data = path.read_bytes()
            mime = "image/jpeg" if file_type == "jpg" else f"image/{file_type}"
            try:
                res = await self.gemini.ocr_image(data, mime_type=mime)
                return res.get("text", "")
            except Exception as e:
                logger.warning("OCR failed: %s", e)
                return ""
        if file_type in audio_exts:
            data = path.read_bytes()
            mime = "audio/mpeg" if file_type == "mp3" else f"audio/{file_type}"
            try:
                res = await self.gemini.transcribe_audio(data, mime_type=mime)
                return res.get("text", "")
            except Exception as e:
                logger.warning("Transcription failed: %s", e)
                return ""
        if file_type == "pdf":
            data = path.read_bytes()
            prompt = (
                "Extract the full text content of this PDF document. Preserve headings and structure. "
                "If the document has more than 100 pages, respond ONLY as JSON: "
                '{"chapters":[{"title":"...","content":"...","summary":"..."}]}. '
                "Otherwise return the plain extracted text."
            )
            try:
                res = await self.gemini.generate_text(prompt=prompt)
                txt = res.get("text", "")
            except Exception as e:
                logger.warning("PDF extract failed: %s", e)
                txt = ""
            try:
                parsed = json.loads(txt.strip().strip("`"))
                if isinstance(parsed, dict) and "chapters" in parsed:
                    (path.parent / (path.name + ".chapters.json")).write_text(json.dumps(parsed), encoding="utf-8")
                    chs = parsed["chapters"]
                    return chs[0].get("content", "") if chs else txt
            except Exception:
                pass
            return txt
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return ""

    async def _ai_extract(self, text: str, file_name: str, style: str) -> Dict[str, Any]:
        if style == "short":
            task_instr = "Task descriptions: short and punchy (one line each)."
        else:
            task_instr = "Task descriptions: detailed with context (2-3 sentences each)."

        prompt = (
            "You are an assistant that processes a document and returns structured JSON.\n"
            f"Document filename: {file_name}\n"
            f"Document text (truncated):\n{text[:12000]}\n\n"
            "Return ONLY valid JSON with these keys:\n"
            '- title: concise title (no emojis)\n'
            '- summary: 2-4 sentence summary (no emojis)\n'
            '- category: one of [Work, Study, Personal, Research, Other]\n'
            '- tags: array of lowercase pill tags (no emojis, max 8)\n'
            '- content: cleaned markdown version of the document\n'
            '- tasks: array of objects {title, description, priority (low/medium/high), due_date (ISO or null)}\n'
            '- transcription: extracted audio transcript if applicable else ""\n'
            '- ocr_text: extracted OCR text if applicable else ""\n'
            '- chapters: if PDF had >100 pages, array of {title, content, summary}; else null\n\n'
            f"{task_instr}\nNo emojis anywhere. JSON only."
        )
        try:
            res = await self.gemini.generate_text(prompt=prompt, temperature=0.2)
        except Exception as e:
            logger.warning("_ai_extract failed: %s", e)
            return {"title": Path(file_name).stem, "summary": "", "category": "Other", "tags": [], "content": text, "tasks": []}
        txt = res.get("text", "").strip()
        if txt.startswith("```"):
            txt = re.sub(r"^```(?:json)?\n?", "", txt)
            txt = re.sub(r"\n?```$", "", txt)
        try:
            data = json.loads(txt)
        except Exception:
            data = {"title": Path(file_name).stem, "summary": "", "category": "Other", "tags": [], "content": text, "tasks": []}
        for k, v in list(data.items()):
            if isinstance(v, str):
                data[k] = sanitize_emojis(v)
            elif isinstance(v, list):
                data[k] = [sanitize_emojis(x) if isinstance(x, str) else x for x in v]
        for t in data.get("tasks", []) or []:
            if isinstance(t, dict):
                t["title"] = sanitize_emojis(t.get("title", "")) or ""
                t["description"] = sanitize_emojis(t.get("description", "")) or ""
        return data

    async def create_note(self, title: str, content_raw: str = "", category: Optional[str] = None, tags: Optional[List[str]] = None) -> Dict[str, Any]:
        title = sanitize_emojis(title) or "Untitled"
        content_raw = sanitize_emojis(content_raw) or ""
        note_id = new_id()
        async with get_notes_db() as db:
            await db.execute(
                """INSERT INTO notes (id, title, content_raw, content_html, summary, category, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (note_id, title, content_raw, "", "", category, now_iso(), now_iso()),
            )
            if tags:
                for tag in tags:
                    tag = sanitize_emojis(str(tag).lower().strip()) or ""
                    if not tag:
                        continue
                    await db.execute("INSERT OR IGNORE INTO tags (name) VALUES (?)", (tag,))
                    cur = await db.execute("SELECT id FROM tags WHERE name = ?", (tag,))
                    r = await cur.fetchone()
                    if r:
                        await db.execute("INSERT OR IGNORE INTO note_tags (note_id, tag_id) VALUES (?,?)", (note_id, r["id"]))
            await db.commit()
        try:
            emb = await self.gemini.embed_text((title + "\n" + content_raw)[:8000])
            async with get_notes_db() as db:
                await db.execute("INSERT OR REPLACE INTO note_embeddings (note_id, embedding_json) VALUES (?,?)", (note_id, json.dumps(emb.get("embedding", []))))
                await db.commit()
        except Exception:
            pass
        return {"id": note_id, "title": title}

    async def get_note(self, note_id: str) -> Dict[str, Any]:
        async with get_notes_db() as db:
            cur = await db.execute("SELECT * FROM notes WHERE id = ?", (note_id,))
            row = await cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Note not found")
            note = dict(row)
            cur = await db.execute(
                """SELECT t.name FROM tags t JOIN note_tags nt ON nt.tag_id=t.id WHERE nt.note_id=?""",
                (note_id,),
            )
            note["tags"] = [r["name"] for r in await cur.fetchall()]
            cur = await db.execute("SELECT * FROM files WHERE note_id = ?", (note_id,))
            note["files"] = [dict(r) for r in await cur.fetchall()]
            cur = await db.execute("SELECT * FROM tasks WHERE source_note_id = ?", (note_id,))
            note["tasks"] = [dict(r) for r in await cur.fetchall()]
        return note

    async def list_notes(self, trashed: bool = False, category: Optional[str] = None, tag: Optional[str] = None, pinned: Optional[bool] = None, limit: int = 200) -> List[Dict[str, Any]]:
        async with get_notes_db() as db:
            q = "SELECT * FROM notes WHERE is_trashed = ?"
            params: List[Any] = [1 if trashed else 0]
            if category:
                q += " AND category = ?"
                params.append(category)
            if pinned is not None:
                q += " AND is_pinned = ?"
                params.append(1 if pinned else 0)
            q += " ORDER BY updated_at DESC LIMIT ?"
            params.append(limit)
            cur = await db.execute(q, params)
            rows = [dict(r) for r in await cur.fetchall()]
            if tag:
                filtered = []
                for n in rows:
                    c2 = await db.execute(
                        """SELECT 1 FROM tags t JOIN note_tags nt ON nt.tag_id=t.id
                           WHERE nt.note_id=? AND t.name=?""", (n["id"], tag.lower()))
                    if await c2.fetchone():
                        filtered.append(n)
                rows = filtered
        return rows

    async def update_note(self, note_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
        async with get_notes_db() as db:
            cur = await db.execute("SELECT * FROM notes WHERE id = ?", (note_id,))
            row = await cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Note not found")
            old = dict(row)
            await db.execute(
                "INSERT INTO note_versions (note_id, content_raw, saved_at) VALUES (?,?,?)",
                (note_id, old.get("content_raw"), now_iso()),
            )
            title = sanitize_emojis(data.get("title", old["title"])) or old["title"]
            content_raw = sanitize_emojis(data.get("content_raw", old["content_raw"]))
            summary = sanitize_emojis(data.get("summary", old["summary"]))
            category = sanitize_emojis(data.get("category", old["category"]))
            is_pinned = data.get("is_pinned", old["is_pinned"])
            await db.execute(
                """UPDATE notes SET title=?, content_raw=?, summary=?, category=?, is_pinned=?, updated_at=? WHERE id=?""",
                (title, content_raw, summary, category, 1 if is_pinned else 0, now_iso(), note_id),
            )
            if "tags" in data:
                await db.execute("DELETE FROM note_tags WHERE note_id = ?", (note_id,))
                for tag in data["tags"] or []:
                    tag = sanitize_emojis(str(tag).lower().strip()) or ""
                    if not tag:
                        continue
                    await db.execute("INSERT OR IGNORE INTO tags (name) VALUES (?)", (tag,))
                    cur = await db.execute("SELECT id FROM tags WHERE name = ?", (tag,))
                    r = await cur.fetchone()
                    if r:
                        await db.execute("INSERT OR IGNORE INTO note_tags (note_id, tag_id) VALUES (?,?)", (note_id, r["id"]))
            await db.commit()
        try:
            emb = await self.gemini.embed_text((title + "\n" + (content_raw or ""))[:8000])
            async with get_notes_db() as db:
                await db.execute("INSERT OR REPLACE INTO note_embeddings (note_id, embedding_json) VALUES (?,?)", (note_id, json.dumps(emb.get("embedding", []))))
                await db.commit()
        except Exception:
            pass
        return {"id": note_id, "title": title, "updated": True}

    async def delete_note(self, note_id: str, permanent: bool = False) -> Dict[str, Any]:
        if permanent:
            async with get_notes_db() as db:
                cur = await db.execute("SELECT file_path FROM files WHERE note_id = ?", (note_id,))
                for r in await cur.fetchall():
                    if r["file_path"]:
                        try:
                            Path(r["file_path"]).unlink(missing_ok=True)
                        except Exception:
                            pass
                await db.execute("DELETE FROM notes WHERE id = ?", (note_id,))
                await db.commit()
            return {"id": note_id, "deleted": "permanent"}
        async with get_notes_db() as db:
            await db.execute("UPDATE notes SET is_trashed=1, trashed_at=? WHERE id=?", (now_iso(), note_id))
            await db.execute("UPDATE tasks SET source_note_id=NULL WHERE source_note_id=?", (note_id,))
            await db.commit()
        return {"id": note_id, "deleted": "soft"}

    async def restore_note(self, note_id: str) -> Dict[str, Any]:
        async with get_notes_db() as db:
            await db.execute("UPDATE notes SET is_trashed=0, trashed_at=NULL WHERE id=?", (note_id,))
            await db.commit()
        return {"id": note_id, "restored": True}

    async def purge_trash(self, older_than_days: int = 30) -> Dict[str, Any]:
        async with get_notes_db() as db:
            cur = await db.execute(
                "SELECT id FROM notes WHERE is_trashed=1 AND (trashed_at IS NULL OR trashed_at <= datetime('now', ?))",
                (f"-{older_than_days} days",),
            )
            ids = [r["id"] for r in await cur.fetchall()]
            for nid in ids:
                await db.execute("DELETE FROM notes WHERE id=?", (nid,))
            await db.commit()
        return {"purged": len(ids)}

    async def list_versions(self, note_id: str) -> List[Dict[str, Any]]:
        async with get_notes_db() as db:
            cur = await db.execute("SELECT * FROM note_versions WHERE note_id=? ORDER BY saved_at DESC", (note_id,))
            return [dict(r) for r in await cur.fetchall()]

    async def rollback(self, note_id: str, version_id: int) -> Dict[str, Any]:
        async with get_notes_db() as db:
            cur = await db.execute("SELECT content_raw FROM note_versions WHERE id=? AND note_id=?", (version_id, note_id))
            r = await cur.fetchone()
            if not r:
                raise HTTPException(status_code=404, detail="Version not found")
            cur2 = await db.execute("SELECT content_raw FROM notes WHERE id=?", (note_id,))
            currow = await cur2.fetchone()
            if currow:
                await db.execute("INSERT INTO note_versions (note_id, content_raw, saved_at) VALUES (?,?,?)", (note_id, currow["content_raw"], now_iso()))
            await db.execute("UPDATE notes SET content_raw=?, updated_at=? WHERE id=?", (r["content_raw"], now_iso(), note_id))
            await db.commit()
        return {"id": note_id, "rolled_back_to": version_id}

    # ------------------------------------------------------------------
    # Wiki-links, outline, checklist
    # ------------------------------------------------------------------
    async def resolve_wiki_links(self, content_raw: str) -> List[Dict[str, Any]]:
        links = []
        for m in re.finditer(r"\[\[(.+?)\]\]", content_raw or ""):
            title = m.group(1).strip()
            async with get_notes_db() as db:
                cur = await db.execute("SELECT id, title FROM notes WHERE title = ? AND is_trashed=0", (title,))
                r = await cur.fetchone()
            links.append({"title": title, "note_id": r["id"] if r else None, "resolved": bool(r)})
        return links

    async def outline(self, note_id: str) -> List[Dict[str, Any]]:
        note = await self.get_note(note_id)
        content = note.get("content_raw") or ""
        items = []
        for line in content.splitlines():
            m = re.match(r"^(#{1,6})\s+(.+)$", line.strip())
            if m:
                items.append({"level": len(m.group(1)), "text": m.group(2).strip(), "anchor": re.sub(r"[^a-z0-9-]", "", m.group(2).lower().replace(" ", "-"))})
        return items

    async def toggle_checklist(self, note_id: str, item_index: int) -> Dict[str, Any]:
        note = await self.get_note(note_id)
        content = note.get("content_raw") or ""
        lines = content.splitlines()
        counter = 0
        for i, line in enumerate(lines):
            m = re.match(r"^(\s*[-*]\s+)\[([ xX])\](.*)$", line)
            if m:
                if counter == item_index:
                    checked = m.group(2).lower() == "x"
                    new_mark = " " if checked else "x"
                    lines[i] = f"{m.group(1)}[{new_mark}]{m.group(3)}"
                    break
                counter += 1
        new_content = "\n".join(lines)
        await self.update_note(note_id, {"content_raw": new_content})
        return {"note_id": note_id, "item_index": item_index, "updated": True}

    # ------------------------------------------------------------------
    # URL scraping
    # ------------------------------------------------------------------
    async def from_url(self, url: str) -> Dict[str, Any]:
        import urllib.request
        def _fetch():
            try:
                with urllib.request.urlopen(url, timeout=20) as resp:
                    raw = resp.read()
                    ctype = resp.headers.get("Content-Type", "")
                    return raw, ctype
            except Exception as e:
                raise HTTPException(status_code=400, detail=f"Fetch failed: {e}")
        raw, ctype = await asyncio.to_thread(_fetch)
        text = raw.decode("utf-8", errors="replace")
        # strip tags
        text = re.sub(r"<script.*?</script>", "", text, flags=re.S | re.I)
        text = re.sub(r"<style.*?</style>", "", text, flags=re.S | re.I)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()[:15000]

        prompt = (
            f"URL: {url}\n\nExtracted text:\n{text}\n\n"
            "Create a clean markdown note from this article. Return JSON with keys: "
            "title, summary, category, tags (array), content (markdown), tasks (array of {title,description,priority,due_date}). No emojis."
        )
        try:
            res = await self.gemini.generate_text(prompt=prompt, temperature=0.2)
            txt = res.get("text", "").strip()
        except Exception as e:
            logger.warning("from_url ai failed: %s", e)
            txt = ""
        if txt.startswith("```"):
            txt = re.sub(r"^```(?:json)?\n?", "", txt)
            txt = re.sub(r"\n?```$", "", txt)
        try:
            data = json.loads(txt)
        except Exception:
            data = {"title": url, "summary": "", "category": "Research", "tags": [], "content": text, "tasks": []}
        for k, v in list(data.items()):
            if isinstance(v, str):
                data[k] = sanitize_emojis(v)
        title = data.get("title", url)
        content_raw = data.get("content", text)
        note = await self.create_note(title=title, content_raw=content_raw, category=data.get("category"), tags=data.get("tags"))
        # update summary
        await self.update_note(note["id"], {"summary": data.get("summary", ""), "content_raw": content_raw})
        return {"note_id": note["id"], "title": title}

    # ------------------------------------------------------------------
    # Export / Import
    # ------------------------------------------------------------------
    async def export_markdown(self, note_id: str) -> str:
        note = await self.get_note(note_id)
        tags = ", ".join(note.get("tags", []))
        md = f"# {note['title']}\n\n"
        if note.get("summary"):
            md += f"> {note['summary']}\n\n"
        if tags:
            md += f"Tags: {tags}\n\n"
        md += note.get("content_raw") or ""
        return md

    async def export_pdf(self, note_id: str) -> bytes:
        md = await self.export_markdown(note_id)
        return text_to_simple_pdf(md)

    async def import_markdown(self, content: str, title: Optional[str] = None) -> Dict[str, Any]:
        title = title or "Imported Note"
        return await self.create_note(title=title, content_raw=content)

    # ------------------------------------------------------------------
    # Search (vector + FTS)
    # ------------------------------------------------------------------
    async def search(self, query: str, limit: int = 30) -> List[Dict[str, Any]]:
        query = (query or "").strip()
        if not query:
            return []
        results: Dict[str, Dict[str, Any]] = {}

        # FTS
        async with get_notes_db() as db:
            try:
                cur = await db.execute(
                    """SELECT n.id, n.title, n.summary, n.category, n.updated_at,
                              snippet(notes_fts, 1, '<mark>', '</mark>', '...', 12) AS snippet
                       FROM notes_fts f JOIN notes n ON n.rowid = f.rowid
                       WHERE notes_fts MATCH ? AND n.is_trashed=0 LIMIT ?""",
                    (query, limit),
                )
                for r in await cur.fetchall():
                    d = dict(r)
                    d["score"] = 0.5
                    d["source"] = "fts"
                    results[d["id"]] = d
            except Exception as e:
                logger.warning("FTS search failed: %s", e)

        # Vector
        try:
            emb = await self.gemini.embed_text(query)
            qvec = emb.get("embedding", [])
            async with get_notes_db() as db:
                cur = await db.execute("SELECT note_id, embedding_json FROM note_embeddings")
                rows = await cur.fetchall()
                cur2 = await db.execute("SELECT id, title, summary, category, updated_at FROM notes WHERE is_trashed=0")
                note_map = {r["id"]: dict(r) for r in await cur2.fetchall()}
            for r in rows:
                try:
                    vec = json.loads(r["embedding_json"])
                except Exception:
                    continue
                sim = cosine_similarity(qvec, vec)
                if sim < 0.3:
                    continue
                nid = r["note_id"]
                if nid not in note_map:
                    continue
                n = note_map[nid]
                entry = {
                    "id": nid,
                    "title": n["title"],
                    "summary": n["summary"],
                    "category": n["category"],
                    "updated_at": n["updated_at"],
                    "score": round(sim, 4),
                    "source": "vector",
                    "snippet": None,
                }
                if nid in results:
                    results[nid]["score"] = max(results[nid].get("score", 0), sim)
                    results[nid]["source"] = "hybrid"
                else:
                    results[nid] = entry
        except Exception as e:
            logger.warning("Vector search failed: %s", e)

        out = sorted(results.values(), key=lambda x: x.get("score", 0), reverse=True)[:limit]
        return out

    # ------------------------------------------------------------------
    # Tasks (Kanban)
    # ------------------------------------------------------------------
    async def list_tasks(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        async with get_notes_db() as db:
            if status:
                cur = await db.execute("SELECT * FROM tasks WHERE status=? ORDER BY created_at DESC", (status,))
            else:
                cur = await db.execute("SELECT * FROM tasks ORDER BY created_at DESC")
            return [dict(r) for r in await cur.fetchall()]

    async def create_task(self, title: str, description: str = "", priority: str = "medium", due_date: Optional[str] = None, source_note_id: Optional[str] = None) -> Dict[str, Any]:
        task_id = new_id()
        async with get_notes_db() as db:
            await db.execute(
                """INSERT INTO tasks (id, source_note_id, title, description, status, priority, due_date, created_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (task_id, source_note_id, sanitize_emojis(title) or "Task", sanitize_emojis(description) or "", "todo",
                 priority if priority in ("low", "medium", "high") else "medium", due_date, now_iso()),
            )
            await db.commit()
        return {"id": task_id, "title": title}

    async def update_task(self, task_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
        async with get_notes_db() as db:
            cur = await db.execute("SELECT * FROM tasks WHERE id=?", (task_id,))
            row = await cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Task not found")
            old = dict(row)
            status = data.get("status", old["status"])
            if status not in ("todo", "in_progress", "done"):
                status = old["status"]
            priority = data.get("priority", old["priority"])
            if priority not in ("low", "medium", "high"):
                priority = old["priority"]
            title = sanitize_emojis(data.get("title", old["title"])) or old["title"]
            description = sanitize_emojis(data.get("description", old["description"])) or ""
            due_date = data.get("due_date", old["due_date"])
            await db.execute(
                "UPDATE tasks SET title=?, description=?, status=?, priority=?, due_date=? WHERE id=?",
                (title, description, status, priority, due_date, task_id),
            )
            await db.commit()
        return {"id": task_id, "updated": True}

    async def delete_task(self, task_id: str) -> Dict[str, Any]:
        async with get_notes_db() as db:
            await db.execute("DELETE FROM tasks WHERE id=?", (task_id,))
            await db.commit()
        return {"id": task_id, "deleted": True}

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------
    def get_settings(self) -> Dict[str, Any]:
        return load_settings()

    def update_settings(self, data: Dict[str, Any]) -> Dict[str, Any]:
        s = load_settings()
        if "task_description_style" in data:
            s["task_description_style"] = data["task_description_style"]
        save_settings(s)
        return s

def text_to_simple_pdf(text: str) -> bytes:
    lines = text.splitlines() or [""]
    wrapped: List[str] = []
    for line in lines:
        if len(line) <= 95:
            wrapped.append(line)
        else:
            for i in range(0, len(line), 95):
                wrapped.append(line[i:i + 95])
    content_lines = ["BT", "/F1 10 Tf", "50 780 Td", "14 TL"]
    first = True
    for line in wrapped:
        safe = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        if first:
            content_lines.append(f"({safe}) Tj")
            first = False
        else:
            content_lines.append("T*")
            content_lines.append(f"({safe}) Tj")
    content_lines.append("ET")
    stream = "\n".join(content_lines).encode("latin-1", errors="replace")

    objects = []
    objects.append(b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n")
    objects.append(b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n")
    objects.append(
        b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>\nendobj\n"
    )
    objects.append(b"4 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>\nendobj\n")
    objects.append(
        f"5 0 obj\n<< /Length {len(stream)} >>\nstream\n".encode("latin-1")
        + stream + b"\nendstream\nendobj\n"
    )

    pdf = b"%PDF-1.4\n"
    offsets = []
    for obj in objects:
        offsets.append(len(pdf))
        pdf += obj
    xref_pos = len(pdf)
    pdf += f"xref\n0 {len(objects) + 1}\n".encode("latin-1")
    pdf += b"0000000000 65535 f \n"
    for off in offsets:
        pdf += f"{off:010d} 00000 n \n".encode("latin-1")
    pdf += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_pos}\n%%EOF\n"
    ).encode("latin-1")
    return pdf

