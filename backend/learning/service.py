"""Learning Assistant service: hierarchy, syllabus, flashcards, knowledge gaps, cheat sheets."""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from backend.database import get_learning_db
from backend.shared.gemini_service import get_gemini_service, sanitize_emojis
from backend.notes.service import text_to_simple_pdf

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent.parent
STORAGE_DIR = Path(os.getenv("STORAGE_DIR", str(BASE_DIR / "storage")))
LEARNING_SETTINGS_FILE = STORAGE_DIR / "learning_settings.json"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id() -> str:
    return str(uuid.uuid4())


def load_learning_settings() -> Dict[str, Any]:
    try:
        if LEARNING_SETTINGS_FILE.exists():
            return json.loads(LEARNING_SETTINGS_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {"external_knowledge": False, "timer_enabled": True, "task_description_style": "detailed"}


def save_learning_settings(data: Dict[str, Any]) -> None:
    STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    LEARNING_SETTINGS_FILE.write_text(json.dumps(data), encoding="utf-8")


class KaTeXValidator:
    @staticmethod
    def validate(formula: str) -> Dict[str, Any]:
        errors: List[Dict[str, Any]] = []
        if not formula or not formula.strip():
            return {"valid": False, "errors": [{"pos": 0, "msg": "Empty formula"}]}
        depth = 0
        for i, ch in enumerate(formula):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth < 0:
                    errors.append({"pos": i, "msg": "Unexpected closing brace"})
                    depth = 0
        if depth != 0:
            errors.append({"pos": len(formula), "msg": f"Unclosed brace ({depth} open)"})
        bdepth = 0
        for i, ch in enumerate(formula):
            if ch == "[":
                bdepth += 1
            elif ch == "]":
                bdepth -= 1
                if bdepth < 0:
                    errors.append({"pos": i, "msg": "Unexpected closing bracket"})
                    bdepth = 0
        if bdepth != 0:
            errors.append({"pos": len(formula), "msg": f"Unclosed bracket ({bdepth} open)"})
        begins = re.findall(r"\\begin\{(\w+)\}", formula)
        ends = re.findall(r"\\end\{(\w+)\}", formula)
        if begins != ends:
            errors.append({"pos": 0, "msg": f"Mismatched environments: begin={begins} end={ends}"})
        return {"valid": len(errors) == 0, "errors": errors, "formula": formula}


class FormulaDiscrepancyDetector:
    CONCEPTS = {
        "voltage": ["U", "V", "E"],
        "current": ["I", "i"],
        "resistance": ["R", "r"],
        "force": ["F", "f"],
        "energy": ["E", "W"],
    }

    @staticmethod
    def detect(texts: List[str]) -> List[Dict[str, Any]]:
        found: Dict[str, set] = defaultdict(set)
        for text in texts:
            for concept, symbols in FormulaDiscrepancyDetector.CONCEPTS.items():
                for sym in symbols:
                    if re.search(rf"\b{re.escape(sym)}\s*[=≡]", text):
                        found[concept].add(sym)
        discrepancies = []
        for concept, syms in found.items():
            if len(syms) > 1:
                discrepancies.append({
                    "concept": concept,
                    "symbols_found": sorted(syms),
                    "message": f"Conflicting symbols for {concept}: {sorted(syms)}. Please pick a standard.",
                })
        return discrepancies


class LearningService:
    def __init__(self):
        self.gemini = get_gemini_service()

    async def create_subject(self, name: str, color_code: str = "#10B981") -> Dict[str, Any]:
        name = sanitize_emojis(name) or "Subject"
        sid = new_id()
        async with get_learning_db() as db:
            await db.execute("INSERT INTO subjects (id, name, color_code) VALUES (?,?,?)", (sid, name, color_code))
            await db.commit()
        return {"id": sid, "name": name, "color_code": color_code}

    async def list_subjects(self) -> List[Dict[str, Any]]:
        async with get_learning_db() as db:
            cur = await db.execute("SELECT * FROM subjects ORDER BY name")
            return [dict(r) for r in await cur.fetchall()]

    async def get_subject(self, subject_id: str) -> Dict[str, Any]:
        async with get_learning_db() as db:
            cur = await db.execute("SELECT * FROM subjects WHERE id=?", (subject_id,))
            r = await cur.fetchone()
            if not r:
                raise ValueError("Subject not found")
            return dict(r)

    async def create_topic(self, subject_id: str, name: str) -> Dict[str, Any]:
        name = sanitize_emojis(name) or "Topic"
        tid = new_id()
        async with get_learning_db() as db:
            await db.execute("INSERT INTO topics (id, subject_id, name) VALUES (?,?,?)", (tid, subject_id, name))
            await db.commit()
        return {"id": tid, "subject_id": subject_id, "name": name}

    async def list_topics(self, subject_id: Optional[str] = None) -> List[Dict[str, Any]]:
        async with get_learning_db() as db:
            if subject_id:
                cur = await db.execute("SELECT * FROM topics WHERE subject_id=? ORDER BY name", (subject_id,))
            else:
                cur = await db.execute("SELECT * FROM topics ORDER BY name")
            return [dict(r) for r in await cur.fetchall()]
    # ------------------------------------------------------------------
    # Lessons
    # ------------------------------------------------------------------
    async def create_lesson(
        self,
        topic_ids: List[str],
        title: str,
        content_raw: str = "",
        content_html: str = "",
        summary_tldr: str = "",
        lesson_date: Optional[str] = None,
    ) -> Dict[str, Any]:
        title = sanitize_emojis(title) or "Lesson"
        content_raw = sanitize_emojis(content_raw) or ""
        summary_tldr = sanitize_emojis(summary_tldr) or ""
        lid = new_id()
        primary_topic = topic_ids[0] if topic_ids else None
        async with get_learning_db() as db:
            await db.execute(
                """INSERT INTO lessons (id, topic_id, title, content_raw, content_html, summary_tldr, lesson_date)
                   VALUES (?,?,?,?,?,?,?)""",
                (lid, primary_topic, title, content_raw, content_html, summary_tldr, lesson_date),
            )
            for tid in (topic_ids or []):
                await db.execute(
                    "INSERT OR IGNORE INTO lesson_tags (lesson_id, tag_name) VALUES (?,?)",
                    (lid, f"topic:{tid}"),
                )
            await db.commit()
        return {"id": lid, "title": title, "topic_ids": topic_ids}

    async def get_lesson(self, lesson_id: str) -> Dict[str, Any]:
        async with get_learning_db() as db:
            cur = await db.execute("SELECT * FROM lessons WHERE id=?", (lesson_id,))
            r = await cur.fetchone()
            if not r:
                raise ValueError("Lesson not found")
            lesson = dict(r)
            cur = await db.execute("SELECT tag_name FROM lesson_tags WHERE lesson_id=?", (lesson_id,))
            lesson["tags"] = [x["tag_name"] for x in await cur.fetchall()]
        return lesson

    async def list_lessons(self, topic_id: Optional[str] = None) -> List[Dict[str, Any]]:
        async with get_learning_db() as db:
            if topic_id:
                cur = await db.execute("SELECT * FROM lessons WHERE topic_id=? ORDER BY lesson_date", (topic_id,))
            else:
                cur = await db.execute("SELECT * FROM lessons ORDER BY lesson_date")
            return [dict(r) for r in await cur.fetchall()]

    async def update_lesson(self, lesson_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
        async with get_learning_db() as db:
            cur = await db.execute("SELECT * FROM lessons WHERE id=?", (lesson_id,))
            r = await cur.fetchone()
            if not r:
                raise ValueError("Lesson not found")
            old = dict(r)
            title = sanitize_emojis(data.get("title", old["title"])) or old["title"]
            content_raw = sanitize_emojis(data.get("content_raw", old["content_raw"])) or ""
            summary_tldr = sanitize_emojis(data.get("summary_tldr", old["summary_tldr"])) or ""
            await db.execute(
                "UPDATE lessons SET title=?, content_raw=?, summary_tldr=? WHERE id=?",
                (title, content_raw, summary_tldr, lesson_id),
            )
            await db.commit()
        return {"id": lesson_id, "title": title, "updated": True}

    async def delete_lesson(self, lesson_id: str) -> Dict[str, Any]:
        async with get_learning_db() as db:
            await db.execute("DELETE FROM lessons WHERE id=?", (lesson_id,))
            await db.commit()
        return {"id": lesson_id, "deleted": True}

    async def timeline(self) -> Dict[str, Any]:
        async with get_learning_db() as db:
            cur = await db.execute("SELECT * FROM lessons ORDER BY lesson_date")
            lessons = [dict(r) for r in await cur.fetchall()]
        by_date: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        by_week: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for l in lessons:
            d = l.get("lesson_date") or ""
            by_date[d].append(l)
            try:
                dt = datetime.fromisoformat(d)
                week = dt.strftime("%G-W%V")
            except Exception:
                week = "unknown"
            by_week[week].append(l)
        return {"by_date": {k: v for k, v in by_date.items()}, "by_week": {k: v for k, v in by_week.items()}, "total": len(lessons)}

    async def classify_upload(self, text: str, subjects: List[str]) -> Dict[str, Any]:
        prompt = (
            "Classify this lesson content into one of the subjects and a topic name.\n"
            f"Available subjects: {subjects}\n"
            f"Content (truncated): {text[:4000]}\n"
            'Return JSON: {"subject": "...", "topic": "...", "confidence": 0.0-1.0}. No emojis.'
        )
        try:
            res = await self.gemini.generate_text(prompt=prompt, temperature=0.2)
            txt = res.get("text", "").strip()
        except Exception as e:
            logger.warning("classify_upload failed: %s", e)
            return {"subject": subjects[0] if subjects else None, "topic": "General", "confidence": 0.0}
        if txt.startswith("```"):
            txt = re.sub(r"^```(?:json)?\n?", "", txt)
            txt = re.sub(r"\n?```$", "", txt)
        try:
            return json.loads(txt)
        except Exception:
            return {"subject": subjects[0] if subjects else None, "topic": "General", "confidence": 0.0}

    # ------------------------------------------------------------------
    # Syllabus
    # ------------------------------------------------------------------
    async def create_syllabus(self, subject_id: str, exam_title: str, exam_date: str, topics_covered: List[str]) -> Dict[str, Any]:
        sid = new_id()
        async with get_learning_db() as db:
            await db.execute(
                """INSERT INTO syllabi (id, subject_id, exam_title, exam_date, topics_covered_json, study_plan_json, created_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (sid, subject_id, sanitize_emojis(exam_title), exam_date, json.dumps(topics_covered), None, now_iso()),
            )
            await db.commit()
        plan = await self.generate_study_plan(exam_title, exam_date, topics_covered)
        async with get_learning_db() as db:
            await db.execute("UPDATE syllabi SET study_plan_json=? WHERE id=?", (json.dumps(plan), sid))
            await db.commit()
        return {"id": sid, "exam_title": exam_title, "exam_date": exam_date, "study_plan": plan}

    async def generate_study_plan(self, exam_title: str, exam_date: str, topics: List[str]) -> Dict[str, Any]:
        prompt = (
            f"Create a backward-scheduled adaptive study plan for exam '{exam_title}' on {exam_date}.\n"
            f"Topics to cover: {topics}\n"
            "Schedule from today backward to exam date. Return JSON: "
            '{"days":[{"date":"YYYY-MM-DD","tasks":["..."],"focus":"..."}], "summary":"..."}. No emojis.'
        )
        try:
            res = await self.gemini.generate_text(prompt=prompt, temperature=0.3)
            txt = res.get("text", "").strip()
        except Exception as e:
            logger.warning("generate_study_plan failed: %s", e)
            return {"days": [], "summary": ""}
        if txt.startswith("```"):
            txt = re.sub(r"^```(?:json)?\n?", "", txt)
            txt = re.sub(r"\n?```$", "", txt)
        try:
            return json.loads(txt)
        except Exception:
            return {"days": [], "summary": txt}

    async def list_syllabi(self, subject_id: Optional[str] = None) -> List[Dict[str, Any]]:
        async with get_learning_db() as db:
            if subject_id:
                cur = await db.execute("SELECT * FROM syllabi WHERE subject_id=? ORDER BY exam_date", (subject_id,))
            else:
                cur = await db.execute("SELECT * FROM syllabi ORDER BY exam_date")
            return [dict(r) for r in await cur.fetchall()]

    async def get_syllabus(self, syllabus_id: str) -> Dict[str, Any]:
        async with get_learning_db() as db:
            cur = await db.execute("SELECT * FROM syllabi WHERE id=?", (syllabus_id,))
            r = await cur.fetchone()
            if not r:
                raise ValueError("Syllabus not found")
            s = dict(r)
            if s.get("study_plan_json"):
                try:
                    s["study_plan"] = json.loads(s["study_plan_json"])
                except Exception:
                    s["study_plan"] = None
            if s.get("topics_covered_json"):
                try:
                    s["topics_covered"] = json.loads(s["topics_covered_json"])
                except Exception:
                    s["topics_covered"] = []
        return s

    # ------------------------------------------------------------------
    # Flashcards (SM-2)
    # ------------------------------------------------------------------
    async def create_flashcard(self, subject_id: Optional[str], topic_id: Optional[str], front: str, back: str) -> Dict[str, Any]:
        fid = new_id()
        async with get_learning_db() as db:
            await db.execute(
                """INSERT INTO flashcards (id, subject_id, topic_id, front_text, back_text, interval_days, ease_factor, consecutive_correct, next_review_date)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (fid, subject_id, topic_id, sanitize_emojis(front), sanitize_emojis(back), 1, 2.5, 0, datetime.now(timezone.utc).date().isoformat()),
            )
            await db.commit()
        return {"id": fid, "front": front, "back": back}

    async def list_flashcards(self, subject_id: Optional[str] = None) -> List[Dict[str, Any]]:
        async with get_learning_db() as db:
            if subject_id:
                cur = await db.execute("SELECT * FROM flashcards WHERE subject_id=? ORDER BY next_review_date", (subject_id,))
            else:
                cur = await db.execute("SELECT * FROM flashcards ORDER BY next_review_date")
            return [dict(r) for r in await cur.fetchall()]

    async def list_flashcards_due(self, limit: int = 15) -> List[Dict[str, Any]]:
        today = datetime.now(timezone.utc).date().isoformat()
        async with get_learning_db() as db:
            cur = await db.execute(
                "SELECT * FROM flashcards WHERE next_review_date <= ? ORDER BY next_review_date LIMIT ?",
                (today, limit),
            )
            return [dict(r) for r in await cur.fetchall()]

    async def review_flashcard(self, flashcard_id: str, rating: str) -> Dict[str, Any]:
        async with get_learning_db() as db:
            cur = await db.execute("SELECT * FROM flashcards WHERE id=?", (flashcard_id,))
            r = await cur.fetchone()
            if not r:
                raise ValueError("Flashcard not found")
            card = dict(r)
            ef = card["ease_factor"] or 2.5
            interval = card["interval_days"] or 1
            if rating == "again":
                interval = 1
                ef = max(1.3, ef - 0.2)
            elif rating == "hard":
                interval = max(1, round(interval * 1.2))
                ef = max(1.3, ef - 0.05)
            elif rating == "easy":
                interval = max(1, round(interval * ef))
                ef = ef + 0.1
            next_date = (datetime.now(timezone.utc).date() + timedelta(days=int(interval))).isoformat()
            await db.execute(
                "UPDATE flashcards SET interval_days=?, ease_factor=?, next_review_date=? WHERE id=?",
                (interval, ef, next_date, flashcard_id),
            )
            await db.commit()
        return {"id": flashcard_id, "interval_days": interval, "ease_factor": ef, "next_review_date": next_date}

    async def delete_flashcard(self, flashcard_id: str) -> Dict[str, Any]:
        async with get_learning_db() as db:
            await db.execute("DELETE FROM flashcards WHERE id=?", (flashcard_id,))
            await db.commit()
        return {"id": flashcard_id, "deleted": True}

    async def record_concept_result(self, subject_id: Optional[str], concept_name: str, correct: bool) -> Dict[str, Any]:
        concept_name = sanitize_emojis(concept_name) or concept_name
        async with get_learning_db() as db:
            cur = await db.execute(
                "SELECT * FROM knowledge_gaps WHERE subject_id IS ? AND concept_name=?",
                (subject_id, concept_name),
            )
            r = await cur.fetchone()
            if r:
                row = dict(r)
                mistakes = row["consecutive_mistakes"] or 0
                corrects = row["consecutive_correct"] or 0
                if correct:
                    corrects += 1
                    mistakes = 0
                else:
                    mistakes += 1
                    corrects = 0
                is_active = 0 if corrects >= 3 else 1
                await db.execute(
                    """UPDATE knowledge_gaps SET consecutive_mistakes=?, consecutive_correct=?, is_active=?, updated_at=?
                       WHERE id=?""",
                    (mistakes, corrects, is_active, now_iso(), row["id"]),
                )
                await db.commit()
                return {"id": row["id"], "concept": concept_name, "mistakes": mistakes, "corrects": corrects, "is_active": bool(is_active)}
            else:
                mistakes = 0 if correct else 1
                corrects = 1 if correct else 0
                is_active = 0 if corrects >= 3 else 1
                gid = new_id()
                await db.execute(
                    """INSERT INTO knowledge_gaps (id, subject_id, concept_name, consecutive_mistakes, consecutive_correct, is_active, updated_at)
                       VALUES (?,?,?,?,?,?,?)""",
                    (gid, subject_id, concept_name, mistakes, corrects, is_active, now_iso()),
                )
                await db.commit()
                return {"id": gid, "concept": concept_name, "mistakes": mistakes, "corrects": corrects, "is_active": bool(is_active)}

    async def list_knowledge_gaps(self, active_only: bool = True) -> List[Dict[str, Any]]:
        async with get_learning_db() as db:
            if active_only:
                cur = await db.execute("SELECT * FROM knowledge_gaps WHERE is_active=1 ORDER BY updated_at DESC")
            else:
                cur = await db.execute("SELECT * FROM knowledge_gaps ORDER BY updated_at DESC")
            return [dict(r) for r in await cur.fetchall()]

    async def extract_cheat_sheet(self, subject_id: Optional[str], topic_id: Optional[str], text: str) -> List[Dict[str, Any]]:
        prompt = (
            "Extract formulas and laws from this text. Return JSON array of objects: "
            '{"formula_name":"...","formula_latex":"...","explanation":"..."}. '
            "KaTeX-compatible LaTeX. No emojis.\nText:\n" + text[:8000]
        )
        try:
            res = await self.gemini.generate_text(prompt=prompt, temperature=0.2)
        except Exception as e:
            logger.warning("extract_cheat_sheet failed: %s", e)
            return []
        txt = res.get("text", "").strip()
        if txt.startswith("```"):
            txt = re.sub(r"^```(?:json)?\n?", "", txt)
            txt = re.sub(r"\n?```$", "", txt)
        try:
            data = json.loads(txt)
        except Exception:
            data = []
        out = []
        for item in (data if isinstance(data, list) else []):
            out.append({
                "formula_name": sanitize_emojis(item.get("formula_name", "")),
                "formula_latex": item.get("formula_latex", ""),
                "explanation": sanitize_emojis(item.get("explanation", "")),
                "validation": KaTeXValidator.validate(item.get("formula_latex", "")),
            })
        return out

    async def save_cheat_sheet(self, subject_id: Optional[str], topic_id: Optional[str], formula_name: str, formula_latex: str, explanation: str, verified: bool = False) -> Dict[str, Any]:
        cid = new_id()
        async with get_learning_db() as db:
            await db.execute(
                """INSERT INTO cheat_sheets (id, subject_id, topic_id, formula_name, formula_latex, explanation, is_user_verified)
                   VALUES (?,?,?,?,?,?,?)""",
                (cid, subject_id, topic_id, sanitize_emojis(formula_name), formula_latex, sanitize_emojis(explanation), 1 if verified else 0),
            )
            await db.commit()
        return {"id": cid, "verified": verified}

    async def list_cheat_sheets(self, subject_id: Optional[str] = None, verified_only: bool = False) -> List[Dict[str, Any]]:
        async with get_learning_db() as db:
            q = "SELECT * FROM cheat_sheets WHERE 1=1"
            params: List[Any] = []
            if subject_id:
                q += " AND subject_id=?"
                params.append(subject_id)
            if verified_only:
                q += " AND is_user_verified=1"
            cur = await db.execute(q, params)
            return [dict(r) for r in await cur.fetchall()]

    async def verify_cheat_sheet(self, cheat_sheet_id: str) -> Dict[str, Any]:
        async with get_learning_db() as db:
            await db.execute("UPDATE cheat_sheets SET is_user_verified=1 WHERE id=?", (cheat_sheet_id,))
            await db.commit()
        return {"id": cheat_sheet_id, "verified": True}

    async def explain_simpler(self, text: str, context: str = "") -> Dict[str, Any]:
        prompt = (
            "Explain the following highlighted text in a simpler way using technical analogies.\n"
            f"Context: {context[:2000]}\nHighlighted: {text[:500]}\n"
            "Return a concise explanation (no emojis)."
        )
        try:
            res = await self.gemini.generate_text(prompt=prompt, temperature=0.4)
            return {"explanation": sanitize_emojis(res.get("text", ""))}
        except Exception as e:
            logger.warning("explain_simpler failed: %s", e)
            return {"explanation": "Unable to generate explanation at this time."}

    async def check_discrepancies(self) -> List[Dict[str, Any]]:
        async with get_learning_db() as db:
            cur = await db.execute("SELECT content_raw FROM lessons")
            texts = [r["content_raw"] or "" for r in await cur.fetchall()]
            cur = await db.execute("SELECT formula_latex, explanation FROM cheat_sheets")
            for r in await cur.fetchall():
                texts.append((r["formula_latex"] or "") + " " + (r["explanation"] or ""))
        return FormulaDiscrepancyDetector.detect(texts)

    def validate_katex(self, formula: str) -> Dict[str, Any]:
        return KaTeXValidator.validate(formula)

    def export_test_pdf(self, title: str, questions: List[Dict[str, Any]], include_answers: bool = False) -> bytes:
        lines = [title, ""]
        for i, q in enumerate(questions, 1):
            lines.append(f"{i}. {q.get('question_text', '')}")
            opts = q.get("options", [])
            for idx, opt in enumerate(opts):
                lines.append(f"   {chr(65+idx)}. {opt}")
            if include_answers:
                lines.append(f"   Answer: {q.get('correct_answer', '')}")
            lines.append("")
        return text_to_simple_pdf("\n".join(lines))

