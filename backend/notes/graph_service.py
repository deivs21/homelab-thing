"""Knowledge graph (Obsidian-style) service for notes."""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional

from backend.database import get_notes_db
from backend.notes.service import cosine_similarity, new_id

logger = logging.getLogger(__name__)

GROUP_COLORS = [
    "#76C457", "#F5F5F5", "#FAFAFA", "#ECECEC",
]


class GraphService:
    def __init__(self, notes_service=None):
        self.notes_service = notes_service

    async def build_graph(self, threshold: float = 0.72) -> Dict[str, Any]:
        async with get_notes_db() as db:
            cur = await db.execute("SELECT id, title, summary, category, is_pinned, updated_at FROM notes WHERE is_trashed=0")
            notes = [dict(r) for r in await cur.fetchall()]
            cur = await db.execute("SELECT note_id, embedding_json FROM note_embeddings")
            emb_map = {r["note_id"]: r["embedding_json"] for r in await cur.fetchall()}
            cur = await db.execute("SELECT name FROM tags")
            all_tags = [r["name"] for r in await cur.fetchall()]
            # note->tags
            note_tags_map: Dict[str, List[str]] = defaultdict(list)
            cur = await db.execute("""SELECT nt.note_id, t.name FROM note_tags nt JOIN tags t ON t.id=nt.tag_id""")
            for r in await cur.fetchall():
                note_tags_map[r["note_id"]].append(r["name"])
            # files count per note
            cur = await db.execute("SELECT note_id, COUNT(*) c FROM files GROUP BY note_id")
            file_counts = {r["note_id"]: r["c"] for r in await cur.fetchall()}

        # clusters by category
        categories = sorted({n.get("category") or "Other" for n in notes})
        cat_color = {c: GROUP_COLORS[i % len(GROUP_COLORS)] for i, c in enumerate(categories)}

        nodes: List[Dict[str, Any]] = []
        node_ids = set()

        # category nodes
        for c in categories:
            cid = f"cat:{c}"
            nodes.append({
                "id": cid,
                "type": "category",
                "label": c,
                "color": cat_color[c],
                "cluster": c,
                "size": 18,
                "metadata": {"kind": "category"},
            })
            node_ids.add(cid)

        # tag nodes (only tags used)
        used_tags = set()
        for n in notes:
            for t in note_tags_map.get(n["id"], []):
                used_tags.add(t)
        for t in sorted(used_tags):
            tid = f"tag:{t}"
            nodes.append({
                "id": tid,
                "type": "tag",
                "label": t,
                "color": "#94A3B8",
                "cluster": "tags",
                "size": 12,
                "metadata": {"kind": "tag"},
            })
            node_ids.add(tid)

        # note nodes
        for n in notes:
            nid = f"note:{n['id']}"
            cat = n.get("category") or "Other"
            nodes.append({
                "id": nid,
                "type": "note",
                "label": n["title"],
                "color": cat_color.get(cat, "#10B981"),
                "cluster": cat,
                "size": 10 + min(file_counts.get(n["id"], 0), 5),
                "metadata": {
                    "note_id": n["id"],
                    "title": n["title"],
                    "summary": n["summary"],
                    "category": cat,
                    "tags": note_tags_map.get(n["id"], []),
                    "updated_at": n["updated_at"],
                    "pinned": bool(n.get("is_pinned")),
                },
            })
            node_ids.add(nid)

        # edges
        edges: List[Dict[str, Any]] = []
        # note -> category
        for n in notes:
            edges.append({"source": f"note:{n['id']}", "target": f"cat:{n.get('category') or 'Other'}", "weight": 1.0, "type": "belongs"})
        # note -> tag
        for n in notes:
            for t in note_tags_map.get(n["id"], []):
                edges.append({"source": f"note:{n['id']}", "target": f"tag:{t}", "weight": 1.0, "type": "tagged"})
        # note -> note by cosine
        note_ids_list = [n["id"] for n in notes]
        for i in range(len(note_ids_list)):
            for j in range(i + 1, len(note_ids_list)):
                a = note_ids_list[i]
                b = note_ids_list[j]
                ea = emb_map.get(a)
                eb = emb_map.get(b)
                if not ea or not eb:
                    continue
                try:
                    va = json.loads(ea)
                    vb = json.loads(eb)
                except Exception:
                    continue
                sim = cosine_similarity(va, vb)
                if sim >= threshold:
                    edges.append({
                        "source": f"note:{a}",
                        "target": f"note:{b}",
                        "weight": round(sim, 4),
                        "type": "similar",
                    })

        clusters = [{"id": c, "label": c, "color": cat_color[c]} for c in categories]
        clusters.append({"id": "tags", "label": "Tags", "color": "#94A3B8"})

        return {
            "nodes": nodes,
            "edges": edges,
            "clusters": clusters,
            "threshold": threshold,
            "stats": {
                "notes": len(notes),
                "tags": len(used_tags),
                "categories": len(categories),
                "edges": len(edges),
            },
        }
