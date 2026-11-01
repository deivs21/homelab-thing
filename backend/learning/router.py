"""FastAPI router for the Learning Assistant module."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response

from backend.learning.service import LearningService
from backend.learning.quiz_engine import QuizEngine

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/learning", tags=["learning"])

svc = LearningService()
qe = QuizEngine()


# ------------------------------------------------------------------
# Settings
# ------------------------------------------------------------------
@router.get("/settings")
async def get_settings():
    from backend.learning.service import load_learning_settings
    return load_learning_settings()


@router.put("/settings")
async def update_settings(payload: Dict[str, Any]):
    from backend.learning.service import load_learning_settings, save_learning_settings
    s = load_learning_settings()
    for k in ("external_knowledge", "timer_enabled", "task_description_style"):
        if k in payload:
            s[k] = payload[k]
    save_learning_settings(s)
    return s


# ------------------------------------------------------------------
# Subjects & Topics
# ------------------------------------------------------------------
@router.get("/subjects")
async def list_subjects():
    return await svc.list_subjects()


@router.post("/subjects")
async def create_subject(payload: Dict[str, Any]):
    return await svc.create_subject(payload.get("name", "Subject"), payload.get("color_code", "#10B981"))


@router.get("/topics")
async def list_topics(subject_id: Optional[str] = None):
    return await svc.list_topics(subject_id)


@router.post("/topics")
async def create_topic(payload: Dict[str, Any]):
    return await svc.create_topic(payload["subject_id"], payload.get("name", "Topic"))


# ------------------------------------------------------------------
# Lessons
# ------------------------------------------------------------------
@router.get("/lessons")
async def list_lessons(topic_id: Optional[str] = None):
    return await svc.list_lessons(topic_id)


@router.post("/lessons")
async def create_lesson(payload: Dict[str, Any]):
    return await svc.create_lesson(
        topic_ids=payload.get("topic_ids", []),
        title=payload.get("title", "Lesson"),
        content_raw=payload.get("content_raw", ""),
        content_html=payload.get("content_html", ""),
        summary_tldr=payload.get("summary_tldr", ""),
        lesson_date=payload.get("lesson_date"),
    )


@router.get("/lessons/{lesson_id}")
async def get_lesson(lesson_id: str):
    return await svc.get_lesson(lesson_id)


@router.put("/lessons/{lesson_id}")
async def update_lesson(lesson_id: str, payload: Dict[str, Any]):
    return await svc.update_lesson(lesson_id, payload)


@router.delete("/lessons/{lesson_id}")
async def delete_lesson(lesson_id: str):
    return await svc.delete_lesson(lesson_id)


@router.get("/timeline")
async def timeline():
    return await svc.timeline()


@router.post("/classify")
async def classify(payload: Dict[str, Any]):
    subjects = payload.get("subjects", [])
    text = payload.get("text", "")
    return await svc.classify_upload(text, subjects)


# ------------------------------------------------------------------
# Syllabus
# ------------------------------------------------------------------
@router.get("/syllabi")
async def list_syllabi(subject_id: Optional[str] = None):
    return await svc.list_syllabi(subject_id)


@router.post("/syllabi")
async def create_syllabus(payload: Dict[str, Any]):
    return await svc.create_syllabus(
        subject_id=payload["subject_id"],
        exam_title=payload.get("exam_title", "Exam"),
        exam_date=payload["exam_date"],
        topics_covered=payload.get("topics_covered", []),
    )


@router.get("/syllabi/{syllabus_id}")
async def get_syllabus(syllabus_id: str):
    return await svc.get_syllabus(syllabus_id)
# ------------------------------------------------------------------
# Quizzes
# ------------------------------------------------------------------
@router.post("/quizzes/generate")
async def generate_quiz(payload: Dict[str, Any]):
    settings = {}
    try:
        from backend.learning.service import load_learning_settings
        settings = load_learning_settings()
    except Exception:
        pass
    return await qe.generate_quiz(
        subject_id=payload.get("subject_id"),
        topic_id=payload.get("topic_id"),
        lesson_ids=payload.get("lesson_ids", []),
        title=payload.get("title"),
        question_count=payload.get("question_count"),
        timer_enabled=payload.get("timer_enabled", settings.get("timer_enabled", True)),
        external_knowledge=payload.get("external_knowledge", settings.get("external_knowledge", False)),
        math_mode=payload.get("math_mode", "value"),
    )


@router.post("/quizzes/{quiz_id}/answer")
async def answer_question(quiz_id: str, payload: Dict[str, Any]):
    return await qe.evaluate_answer(
        quiz_id=quiz_id,
        question_id=payload["question_id"],
        user_answer=payload.get("user_answer"),
        math_mode=payload.get("math_mode", "value"),
    )


@router.post("/quizzes/{quiz_id}/close")
async def close_quiz(quiz_id: str):
    return await qe.close_quiz(quiz_id)


@router.get("/quizzes/{quiz_id}/bubbles")
async def bubbles(quiz_id: str):
    return qe.get_bubble_states(quiz_id)


@router.get("/quizzes/attempts")
async def list_attempts(quiz_id: Optional[str] = None):
    return await qe.list_attempts(quiz_id)


@router.get("/quizzes/morning")
async def morning_quiz():
    return await qe.morning_quiz()


# ------------------------------------------------------------------
# Flashcards
# ------------------------------------------------------------------
@router.get("/flashcards")
async def list_flashcards(subject_id: Optional[str] = None):
    return await svc.list_flashcards(subject_id)


@router.get("/flashcards/due")
async def due_flashcards(limit: int = Query(15)):
    return await svc.list_flashcards_due(limit)


@router.post("/flashcards")
async def create_flashcard(payload: Dict[str, Any]):
    return await svc.create_flashcard(payload.get("subject_id"), payload.get("topic_id"), payload.get("front", ""), payload.get("back", ""))


@router.post("/flashcards/{flashcard_id}/review")
async def review_flashcard(flashcard_id: str, payload: Dict[str, Any]):
    return await svc.review_flashcard(flashcard_id, payload.get("rating", "hard"))


@router.delete("/flashcards/{flashcard_id}")
async def delete_flashcard(flashcard_id: str):
    return await svc.delete_flashcard(flashcard_id)


# ------------------------------------------------------------------
# Knowledge Gaps
# ------------------------------------------------------------------
@router.get("/knowledge-gaps")
async def list_gaps(active_only: bool = Query(True)):
    return await svc.list_knowledge_gaps(active_only)


@router.post("/knowledge-gaps/record")
async def record_gap(payload: Dict[str, Any]):
    return await svc.record_concept_result(payload.get("subject_id"), payload.get("concept_name", ""), bool(payload.get("correct", False)))


# ------------------------------------------------------------------
# Cheat Sheets
# ------------------------------------------------------------------
@router.post("/cheat-sheets/extract")
async def extract_cs(payload: Dict[str, Any]):
    return await svc.extract_cheat_sheet(payload.get("subject_id"), payload.get("topic_id"), payload.get("text", ""))


@router.get("/cheat-sheets")
async def list_cs(subject_id: Optional[str] = None, verified_only: bool = Query(False)):
    return await svc.list_cheat_sheets(subject_id, verified_only)


@router.post("/cheat-sheets")
async def save_cs(payload: Dict[str, Any]):
    return await svc.save_cheat_sheet(
        payload.get("subject_id"), payload.get("topic_id"),
        payload.get("formula_name", ""), payload.get("formula_latex", ""),
        payload.get("explanation", ""), payload.get("verified", False),
    )


@router.post("/cheat-sheets/{cs_id}/verify")
async def verify_cs(cs_id: str):
    return await svc.verify_cheat_sheet(cs_id)


# ------------------------------------------------------------------
# KaTeX validation, discrepancies, explain
# ------------------------------------------------------------------
@router.post("/katex/validate")
async def validate_katex(payload: Dict[str, Any]):
    return svc.validate_katex(payload.get("formula", ""))


@router.get("/discrepancies")
async def discrepancies():
    return await svc.check_discrepancies()


@router.post("/explain")
async def explain(payload: Dict[str, Any]):
    return await svc.explain_simpler(payload.get("text", ""), payload.get("context", ""))


# ------------------------------------------------------------------
# PDF export
# ------------------------------------------------------------------
@router.post("/export/test-pdf")
async def export_test_pdf(payload: Dict[str, Any]):
    data = svc.export_test_pdf(payload.get("title", "Test"), payload.get("questions", []), payload.get("include_answers", False))
    return Response(content=data, media_type="application/pdf",
                   headers={"Content-Disposition": "attachment; filename=test.pdf"})

