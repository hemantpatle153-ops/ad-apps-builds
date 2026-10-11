"""Reading and writing the published files (a checkout of the feed-data branch)."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

MAX_INDEX = 600
MAX_REVIEW = 300
KEEP_DAYS = 60


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class Store:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def read(self, rel: str, default: Any = None) -> Any:
        p = self.root / rel
        if not p.exists():
            return default
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return default

    def write(self, rel: str, obj: Any) -> None:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        tmp.replace(p)

    def exists(self, rel: str) -> bool:
        return (self.root / rel).exists()

    # ---- jobs -------------------------------------------------------------
    def add_post(self, post: dict[str, Any], summary: dict[str, Any], *, now: str | None = None) -> None:
        self.write(f"jobs/posts/{post['id']}.json", {"schema": 1, **post})
        index = self.read("jobs/index.json") or {"schema": 1, "posts": []}
        posts = [p for p in index.get("posts", []) if p.get("id") != post["id"]]
        posts.insert(0, summary)
        posts.sort(key=lambda p: p.get("postedAt") or "", reverse=True)
        for old in posts[MAX_INDEX:]:
            (self.root / f"jobs/posts/{old['id']}.json").unlink(missing_ok=True)
        self.write("jobs/index.json", {"schema": 1, "generatedAt": now or _now(), "posts": posts[:MAX_INDEX]})

    def add_review(self, entry: dict[str, Any]) -> None:
        items = self.read("review/index.json") or []
        items = [e for e in items if e.get("key") != entry.get("key")]
        items.insert(0, entry)
        self.write("review/index.json", items[:MAX_REVIEW])

    # ---- quiz -------------------------------------------------------------
    def quiz_index(self) -> dict[str, Any]:
        return self.read("quiz/index.json") or {"schema": 1, "daily": [], "currentAffairs": [], "banks": []}

    def save_quiz_index(self, idx: dict[str, Any], *, now: str | None = None) -> None:
        idx["schema"] = 1
        idx["generatedAt"] = now or _now()
        for key in ("daily", "currentAffairs"):
            idx[key] = sorted(set(idx.get(key) or []), reverse=True)[:KEEP_DAYS]
        self.write("quiz/index.json", idx)

    def bank(self, subject: str) -> list[dict[str, Any]]:
        return (self.read(f"quiz/banks/{subject}.json") or {}).get("questions", [])

    def save_bank(self, subject: str, questions: list[dict[str, Any]], title: dict[str, str]) -> None:
        self.write(f"quiz/banks/{subject}.json", {"schema": 1, "subject": subject, "questions": questions})
        idx = self.quiz_index()
        banks = [b for b in idx.get("banks", []) if b.get("id") != subject]
        banks.append({"id": subject, "title": title, "count": len(questions),
                      "file": f"quiz/banks/{subject}.json", "updatedAt": _now()})
        idx["banks"] = sorted(banks, key=lambda b: b["id"])
        self.save_quiz_index(idx)

    def prune_days(self, folder: str, keep: list[str]) -> None:
        d = self.root / folder
        if not d.exists():
            return
        keep_set = set(keep)
        for f in d.glob("*.json"):
            if f.stem not in keep_set:
                f.unlink()
