"""Quiz engine: generation, evaluation, scoring, stepper state."""

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from backend.database import get_learning_db
from backend.shared.gemini_service import get_gemini_service, sanitize_emojis

logger = logging.getLogger(__name__)
GEMINI = get_gemini_service()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id() -> str:
    return str(uuid.uuid4())


class QuizEngine:
    def __init__(self):
        self.gemini = GEMINI
        self.sessions: Dict[str, Dict[str, Any]] = {}

    async def generate_quiz(
        self,
        subject_id: Optional[str],
        topic_id: Optional[str],
        lesson_ids: List[str],
        title: Optional[str] = None,
        question_count: Optional[int] = None,
        timer_enabled: bool = True,
        external_knowledge: bool = False,
        math_mode: str = "value",
    ) -> Dict[str, Any]:
        async with get_learning_db() as db:
            lessons = []
            if lesson_ids:
                for lid in lesson_ids:
                    cur = await db.execute("SELECT * FROM lessons WHERE id=?", (lid,))
                    r = await cur.fetchone()
                    if r:
                        lessons.append(dict(r))
            else:
                if topic_id:
                    cur = await db.execute("SELECT * FROM lessons WHERE topic_id=?", (topic_id,))
                else:
                    cur = await db.execute("SELECT * FROM lessons")
                lessons = [dict(r) for r in await cur.fetchall()]

        content = "\n\n".join(f"## {l['title']}\n{l.get('content_raw','')}" for l in lessons)[:15000]
        ext = "You may use external knowledge." if external_knowledge else "Use ONLY the provided material. No external knowledge."

        prompt = (
            "Generate a quiz from the material below. " + ext + "\n"
            f"Math mode: {math_mode}.\nDecide question count (5-15). "
            'Return JSON: {"title":..,"time_limit_seconds":int|null,"questions":[{"question_type":"single_choice|multiple_choice|open_text|math_calculation","question_text":str,"options":[str],"correct_answer":str,"explanation_steps":str,"hint_text":str,"source_reference":str,"theory_callout":str,"analogy":str}]}. '
            "Material:\n" + content + "\nJSON only, no emojis."
        )
        try:
            res = await self.gemini.generate_text(prompt=prompt, temperature=0.3)
            txt = res.get("text", "").strip()
        except Exception as e:
            logger.warning("generate_quiz failed: %s", e)
            txt = ""
        if txt.startswith("```"):
            txt = re.sub(r"^```(?:json)?\n?", "", txt)
            txt = re.sub(r"\n?```$", "", txt)
        try:
            data = json.loads(txt)
        except Exception:
            data = {"title": title or "Quiz", "time_limit_seconds": None, "questions": []}

        q_title = sanitize_emojis(data.get("title") or title or "Quiz")
        time_limit = data.get("time_limit_seconds")
        if not timer_enabled:
            time_limit = None
        questions = data.get("questions", [])
        if question_count:
            questions = questions[:question_count]

        quiz_id = new_id()
        async with get_learning_db() as db:
            await db.execute(
                "INSERT INTO quizzes (id, subject_id, topic_id, title, time_limit_seconds, created_at) VALUES (?,?,?,?,?,?)",
                (quiz_id, subject_id, topic_id, q_title, time_limit, now_iso()),
            )
            for idx, q in enumerate(questions):
                qid = new_id()
                await db.execute(
                    "INSERT INTO quiz_questions (id, quiz_id, question_order, question_type, question_text, media_url, options_json, correct_answer, explanation_steps, hint_text, source_reference) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (qid, quiz_id, idx, q.get("question_type", "single_choice"),
                     sanitize_emojis(q.get("question_text", "")), None,
                     json.dumps(q.get("options", [])),
                     sanitize_emojis(str(q.get("correct_answer", ""))),
                     sanitize_emojis(q.get("explanation_steps", "")),
                     sanitize_emojis(q.get("hint_text", "")),
                     sanitize_emojis(q.get("source_reference", ""))),
                )
            await db.commit()

        q_list = []
        async with get_learning_db() as db:
            cur = await db.execute("SELECT * FROM quiz_questions WHERE quiz_id=? ORDER BY question_order", (quiz_id,))
            for r in await cur.fetchall():
                qd = dict(r)
                qd["options"] = json.loads(qd["options_json"] or "[]")
                gen = questions[qd["question_order"]] if qd["question_order"] < len(questions) else {}
                qd["theory_callout"] = sanitize_emojis(gen.get("theory_callout", ""))
                qd["analogy"] = sanitize_emojis(gen.get("analogy", ""))
                q_list.append(qd)

        self.sessions[quiz_id] = {
            "quiz_id": quiz_id,
            "title": q_title,
            "time_limit_seconds": time_limit,
            "questions": q_list,
            "current_index": 0,
            "answers": {},
            "bubble_states": {q["id"]: 4 for q in q_list},
            "started_at": now_iso(),
        }
        if q_list:
            self.sessions[quiz_id]["bubble_states"][q_list[0]["id"]] = 2

        return {
            "quiz_id": quiz_id,
            "title": q_title,
            "time_limit_seconds": time_limit,
            "questions": [{"id": q["id"], "order": q["question_order"], "type": q["question_type"], "text": q["question_text"], "options": q["options"], "media_url": q["media_url"]} for q in q_list],
            "bubble_states": self.sessions[quiz_id]["bubble_states"],
        }

    def get_session(self, quiz_id: str) -> Dict[str, Any]:
        return self.sessions.get(quiz_id)
    async def evaluate_answer(self, quiz_id: str, question_id: str, user_answer: Any, math_mode: str = "value") -> Dict[str, Any]:
        session = self.sessions.get(quiz_id)
        if not session:
            session = {"quiz_id": quiz_id, "questions": [], "answers": {}, "bubble_states": {}}
            self.sessions[quiz_id] = session
        question = None
        for q in session.get("questions", []):
            if q["id"] == question_id:
                question = q
                break
        if question is None:
            async with get_learning_db() as db:
                cur = await db.execute("SELECT * FROM quiz_questions WHERE id=?", (question_id,))
                r = await cur.fetchone()
                if not r:
                    raise ValueError("Question not found")
                question = dict(r)
                question["options"] = json.loads(question["options_json"] or "[]")
                session["questions"].append(question)

        qtype = question["question_type"]
        correct_answer = str(question["correct_answer"]).strip()
        options = question["options"]

        score = 0.0
        is_correct = False

        if qtype == "single_choice":
            ua = str(user_answer).strip()
            try:
                idx = int(ua)
                selected_text = options[idx] if 0 <= idx < len(options) else ua
            except Exception:
                selected_text = ua
            try:
                ci = int(correct_answer)
                correct_text = options[ci] if 0 <= ci < len(options) else correct_answer
            except Exception:
                correct_text = correct_answer
            if selected_text.strip().lower() == correct_text.strip().lower():
                is_correct = True
                score = 1.0

        elif qtype == "multiple_choice":
            ua = user_answer if isinstance(user_answer, list) else [user_answer]
            selected = set()
            for item in ua:
                s = str(item).strip()
                try:
                    i = int(s)
                    selected.add(options[i] if 0 <= i < len(options) else s)
                except Exception:
                    selected.add(s)
            correct_set = set()
            for part in correct_answer.split(","):
                part = part.strip()
                try:
                    i = int(part)
                    correct_set.add(options[i] if 0 <= i < len(options) else part)
                except Exception:
                    correct_set.add(part)
            if selected == correct_set:
                is_correct = True
                score = 1.0
            elif selected & correct_set:
                score = 0.5
            else:
                score = 0.0

        elif qtype == "open_text":
            ua = str(user_answer).strip().lower().rstrip(".")
            ca = correct_answer.strip().lower().rstrip(".")
            if ua == ca:
                is_correct = True
                score = 1.0
            else:
                try:
                    judge_prompt = (
                        "Compare the user answer to the correct answer. "
                        'Return JSON: {"verdict":"correct"|"minor_slip"|"wrong", "reason":"..."}. \n'
                        f"Correct: {correct_answer}\nUser: {user_answer}\nNo emojis."
                    )
                    jr = await self.gemini.generate_text(prompt=judge_prompt, temperature=0.1)
                    jtxt = jr.get("text", "").strip()
                    if jtxt.startswith("```"):
                        jtxt = re.sub(r"^```(?:json)?\n?", "", jtxt)
                        jtxt = re.sub(r"\n?```$", "", jtxt)
                    jd = json.loads(jtxt)
                    verdict = jd.get("verdict", "wrong")
                    if verdict == "correct":
                        is_correct = True
                        score = 1.0
                    elif verdict == "minor_slip":
                        score = 0.5
                    else:
                        score = 0.0
                except Exception:
                    score = 0.0

        elif qtype == "math_calculation":
            try:
                ua_num = float(str(user_answer).strip().replace(",", "."))
                ca_num = float(correct_answer.strip().replace(",", "."))
                if abs(ua_num - ca_num) < 1e-6 * max(1, abs(ca_num)):
                    is_correct = True
                    score = 1.0
                elif abs(ua_num - ca_num) <= 0.05 * max(1, abs(ca_num)):
                    score = 0.5
                else:
                    score = 0.0
            except Exception:
                if math_mode == "derivation":
                    try:
                        jp = (
                            "Evaluate this math solution. Return JSON {\"score\":0|0.5|1.0,\"reason\":\"...\"}.\n"
                            f"Correct answer: {correct_answer}\nUser solution: {user_answer}\nNo emojis."
                        )
                        jr = await self.gemini.generate_text(prompt=jp, temperature=0.1)
                        jtxt = jr.get("text", "").strip()
                        if jtxt.startswith("```"):
                            jtxt = re.sub(r"^```(?:json)?\n?", "", jtxt)
                            jtxt = re.sub(r"\n?```$", "", jtxt)
                        jd = json.loads(jtxt)
                        score = float(jd.get("score", 0.0))
                        is_correct = score >= 1.0
                    except Exception:
                        score = 0.0
                else:
                    score = 0.0

        if quiz_id in self.sessions:
            sess = self.sessions[quiz_id]
            sess["bubble_states"][question_id] = 1 if is_correct else 3
            sess["answers"][question_id] = {"user_answer": user_answer, "correct": is_correct, "score": score}

        hint = question.get("hint_text", "")
        theory = question.get("theory_callout", "") or ""
        analogy = question.get("analogy", "") or ""
        source = question.get("source_reference", "") or ""
        steps = question.get("explanation_steps", "") or ""

        feedback = {
            "correct": is_correct,
            "score": score,
            "message": "Atbilde pareiza!" if is_correct else "Atbilde nepareiza, Tev vēl jāmācās!",
            "hint": hint,
            "padoms": hint,
            "retry_allowed": True,
            "next_allowed": True,
            "atbildet_velreiz": True,
            "nakamais_jautajums": True,
            "bubble_state": 1 if is_correct else 3,
            "solution": {
                "steps": steps,
                "theory": theory,
                "analogy": analogy,
                "note_snippet": source,
            },
        }
        return feedback

    async def close_quiz(self, quiz_id: str) -> Dict[str, Any]:
        session = self.sessions.get(quiz_id)
        if not session:
            return {"quiz_id": quiz_id, "saved": False}
        answers = session.get("answers", {})
        total = len(session.get("questions", []))
        score_sum = sum(a.get("score", 0.0) for a in answers.values())
        percent = (score_sum / total * 100.0) if total else 0.0
        async with get_learning_db() as db:
            await db.execute(
                "INSERT INTO quiz_attempts (id, quiz_id, score_percent, user_answers_json, completed_at) VALUES (?,?,?,?,?)",
                (new_id(), quiz_id, round(percent, 2), json.dumps(answers, default=str), now_iso()),
            )
            await db.commit()
        del self.sessions[quiz_id]
        return {"quiz_id": quiz_id, "score_percent": round(percent, 2), "saved": True}

    async def morning_quiz(self) -> Dict[str, Any]:
        async with get_learning_db() as db:
            cur = await db.execute("SELECT id FROM lessons ORDER BY lesson_date DESC LIMIT 5")
            lesson_ids = [r["id"] for r in await cur.fetchall()]
        quiz = await self.generate_quiz(None, None, lesson_ids, question_count=3, timer_enabled=False, external_knowledge=False)
        return quiz

    async def list_attempts(self, quiz_id: Optional[str] = None) -> List[Dict[str, Any]]:
        async with get_learning_db() as db:
            if quiz_id:
                cur = await db.execute("SELECT * FROM quiz_attempts WHERE quiz_id=? ORDER BY completed_at DESC", (quiz_id,))
            else:
                cur = await db.execute("SELECT * FROM quiz_attempts ORDER BY completed_at DESC")
            return [dict(r) for r in await cur.fetchall()]

    def get_bubble_states(self, quiz_id: str) -> Dict[str, int]:
        session = self.sessions.get(quiz_id)
        if not session:
            return {}
        return session.get("bubble_states", {})

