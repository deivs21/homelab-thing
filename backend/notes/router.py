"""FastAPI router for the Notes App module."""

from __future__ import annotations

import hashlib
import logging
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import Response

from backend.notes.service import NotesService, TEMP_DIR, UPLOADS_DIR, ensure_dirs
from backend.notes.graph_service import GraphService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/notes", tags=["notes"])

svc = NotesService()
graph_svc = GraphService(svc)


@router.post("/upload/chunk")
async def upload_chunk(
    upload_id: str = Form(...),
    chunk_index: int = Form(...),
    total_chunks: int = Form(...),
    file_name: str = Form(...),
    file: UploadFile = File(...),
):
    chunk = await file.read()
    return await svc.handle_chunk(upload_id, chunk_index, total_chunks, file_name, chunk)


@router.post("/upload/resolve")
async def upload_resolve(payload: Dict[str, Any]):
    upload_id = payload.get("upload_id")
    action = payload.get("action")
    if not upload_id or not action:
        raise HTTPException(status_code=400, detail="upload_id and action required")
    return await svc.resolve_upload(upload_id, action)


@router.post("/paste-image")
async def paste_image(file: UploadFile = File(...)):
    data = await file.read()
    upload_id = str(uuid.uuid4())
    ensure_dirs()
    temp_dir = TEMP_DIR / upload_id
    temp_dir.mkdir(parents=True, exist_ok=True)
    assembled = temp_dir / (file.filename or "image.png")
    assembled.write_bytes(data)
    result = await svc.process_upload(upload_id, assembled, file.filename or "image.png", hashlib.sha256(data).hexdigest(), temp_dir)
    md = f"![{file.filename}](/storage/uploads/{Path(result['file_path']).name})"
    return {"markdown": md, **result}


@router.get("")
async def list_notes(
    trashed: bool = Query(False),
    category: Optional[str] = None,
    tag: Optional[str] = None,
    pinned: Optional[bool] = None,
    limit: int = Query(200),
):
    return await svc.list_notes(trashed=trashed, category=category, tag=tag, pinned=pinned, limit=limit)


@router.post("")
async def create_note(payload: Dict[str, Any]):
    return await svc.create_note(
        title=payload.get("title", "Untitled"),
        content_raw=payload.get("content_raw", ""),
        category=payload.get("category"),
        tags=payload.get("tags"),
    )


@router.get("/search")
async def search_notes(q: str = Query(...), limit: int = Query(30)):
    return await svc.search(q, limit=limit)


@router.get("/graph")
async def graph(threshold: float = Query(0.72)):
    return await graph_svc.build_graph(threshold=threshold)


@router.get("/settings")
async def get_settings():
    return svc.get_settings()


@router.put("/settings")
async def update_settings(payload: Dict[str, Any]):
    return svc.update_settings(payload)


@router.get("/tasks")
async def list_tasks(status: Optional[str] = None):
    return await svc.list_tasks(status=status)


@router.post("/tasks")
async def create_task(payload: Dict[str, Any]):
    return await svc.create_task(
        title=payload.get("title", "Task"),
        description=payload.get("description", ""),
        priority=payload.get("priority", "medium"),
        due_date=payload.get("due_date"),
        source_note_id=payload.get("source_note_id"),
    )
@router.put("/tasks/{task_id}")
async def update_task(task_id: str, payload: Dict[str, Any]):
    return await svc.update_task(task_id, payload)


@router.delete("/tasks/{task_id}")
async def delete_task(task_id: str):
    return await svc.delete_task(task_id)


@router.post("/from-url")
async def from_url(payload: Dict[str, Any]):
    url = payload.get("url")
    if not url:
        raise HTTPException(status_code=400, detail="url required")
    return await svc.from_url(url)


@router.post("/import/md")
async def import_md(payload: Dict[str, Any]):
    content = payload.get("content", "")
    title = payload.get("title")
    return await svc.import_markdown(content, title)


@router.post("/trash/purge")
async def purge_trash(older_than_days: int = Query(30)):
    return await svc.purge_trash(older_than_days=older_than_days)


@router.get("/{note_id}")
async def get_note(note_id: str):
    return await svc.get_note(note_id)


@router.put("/{note_id}")
async def update_note(note_id: str, payload: Dict[str, Any]):
    return await svc.update_note(note_id, payload)


@router.delete("/{note_id}")
async def delete_note(note_id: str, permanent: bool = Query(False)):
    return await svc.delete_note(note_id, permanent=permanent)


@router.post("/{note_id}/restore")
async def restore_note(note_id: str):
    return await svc.restore_note(note_id)


@router.get("/{note_id}/versions")
async def list_versions(note_id: str):
    return await svc.list_versions(note_id)


@router.post("/{note_id}/rollback/{version_id}")
async def rollback(note_id: str, version_id: int):
    return await svc.rollback(note_id, version_id)


@router.get("/{note_id}/outline")
async def outline(note_id: str):
    return await svc.outline(note_id)


@router.post("/{note_id}/checklist/{item_index}/toggle")
async def toggle_checklist(note_id: str, item_index: int):
    return await svc.toggle_checklist(note_id, item_index)


@router.get("/{note_id}/links")
async def wiki_links(note_id: str):
    note = await svc.get_note(note_id)
    return await svc.resolve_wiki_links(note.get("content_raw", ""))


@router.get("/{note_id}/export/md")
async def export_md(note_id: str):
    md = await svc.export_markdown(note_id)
    return Response(content=md, media_type="text/markdown",
                   headers={"Content-Disposition": f"attachment; filename=note_{note_id}.md"})


@router.get("/{note_id}/export/pdf")
async def export_pdf(note_id: str):
    data = await svc.export_pdf(note_id)
    return Response(content=data, media_type="application/pdf",
                   headers={"Content-Disposition": f"attachment; filename=note_{note_id}.pdf"})

