"""Offline SQLite WAL page archive and DOM regression gate for ScreenWeave.

Archives canonical DOM summaries with SHA-256 fingerprints and evaluates DOM
regressions using ``diff_snapshots`` semantics before fragile action sequences run.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

try:
    from .local_browser import diff_snapshots
except ImportError:
    from local_browser import diff_snapshots


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _canonical_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _extract_element_keys(dom_summary: dict[str, Any]) -> list[str]:
    """Extract deterministic element/selector identifiers from a DOM summary."""
    keys: list[str] = []
    seen: set[str] = set()

    raw_selectors = dom_summary.get("selectors")
    if isinstance(raw_selectors, list):
        for sel in raw_selectors:
            s = str(sel).strip()
            if s and s not in seen:
                seen.add(s)
                keys.append(s)

    interactive = dom_summary.get("interactive")
    if isinstance(interactive, dict):
        samples = interactive.get("samples")
        if isinstance(samples, list):
            for sample in samples:
                if isinstance(sample, dict):
                    tag = str(sample.get("tag") or "").strip().lower()
                    text = str(sample.get("text") or "").strip()
                    selector = str(sample.get("selector") or "").strip()
                    ident = selector or (f"{tag}:{text}" if text else tag)
                else:
                    ident = str(sample).strip()
                if ident and ident not in seen:
                    seen.add(ident)
                    keys.append(ident)

    return keys


class PageArchiveStore:
    """SQLite WAL-backed local archive of DOM snapshots with regression checking."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA synchronous=NORMAL;")
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS page_snapshots (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                snapshot_id TEXT NOT NULL UNIQUE,
                page_key TEXT NOT NULL,
                url TEXT NOT NULL,
                label TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                dom_summary_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        self._conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_page_snapshots_key_label_seq
            ON page_snapshots (page_key, label, seq DESC);
            """
        )
        self._conn.commit()

    def close(self) -> None:
        """Close the underlying SQLite connection."""
        self._conn.close()

    def __enter__(self) -> PageArchiveStore:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def save_snapshot(
        self,
        page_key: str,
        url: str,
        dom_summary: dict[str, Any],
        *,
        label: str = "baseline",
    ) -> str:
        """Persist a DOM summary snapshot and return its unique ``snapshot_id``."""
        key = str(page_key or "").strip()
        if not key:
            raise ValueError("page_key must be a non-empty string")
        if not isinstance(dom_summary, dict):
            raise TypeError("dom_summary must be a dict")
        norm_label = str(label or "baseline").strip() or "baseline"
        norm_url = str(url or dom_summary.get("url") or "").strip()

        canonical = _canonical_json(dom_summary)
        digest = _sha256_hex(canonical)
        snapshot_id = f"snap-{uuid.uuid4().hex[:16]}"
        created_at = _utc_now()

        self._conn.execute(
            """
            INSERT INTO page_snapshots (
                snapshot_id, page_key, url, label, sha256, dom_summary_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?);
            """,
            (snapshot_id, key, norm_url, norm_label, digest, canonical, created_at),
        )
        self._conn.commit()
        return snapshot_id

    def get_latest(
        self,
        page_key: str,
        *,
        label: str | None = None,
    ) -> dict[str, Any] | None:
        """Return the latest archived snapshot for ``page_key`` (optionally filtered by ``label``)."""
        key = str(page_key or "").strip()
        if not key:
            return None
        if label is not None:
            row = self._conn.execute(
                """
                SELECT snapshot_id, page_key, url, label, sha256, dom_summary_json, created_at
                FROM page_snapshots
                WHERE page_key = ? AND label = ?
                ORDER BY seq DESC
                LIMIT 1;
                """,
                (key, str(label).strip()),
            ).fetchone()
        else:
            row = self._conn.execute(
                """
                SELECT snapshot_id, page_key, url, label, sha256, dom_summary_json, created_at
                FROM page_snapshots
                WHERE page_key = ?
                ORDER BY seq DESC
                LIMIT 1;
                """,
                (key,),
            ).fetchone()

        if row is None:
            return None

        return {
            "snapshot_id": row["snapshot_id"],
            "page_key": row["page_key"],
            "url": row["url"],
            "label": row["label"],
            "sha256": row["sha256"],
            "dom_summary": json.loads(row["dom_summary_json"]),
            "created_at": row["created_at"],
        }

    def check_dom_regression(
        self,
        page_key: str,
        current_dom_summary: dict[str, Any],
        *,
        max_changed_ratio: float = 0.25,
        baseline_label: str = "baseline",
    ) -> dict[str, Any]:
        """Compare ``current_dom_summary`` against the latest archived baseline for ``page_key``.

        Returns a structured gate verdict including ``changed_ratio``, ``missing_selectors``,
        ``added_elements``, ``diff``, and ``passed`` (``changed_ratio <= max_changed_ratio``).
        """
        if not isinstance(current_dom_summary, dict):
            raise TypeError("current_dom_summary must be a dict")
        if max_changed_ratio < 0.0 or max_changed_ratio > 1.0:
            raise ValueError("max_changed_ratio must be between 0.0 and 1.0")

        baseline = self.get_latest(page_key, label=baseline_label)
        if baseline is None:
            return {
                "passed": False,
                "has_baseline": False,
                "page_key": str(page_key),
                "baseline_label": baseline_label,
                "max_changed_ratio": max_changed_ratio,
                "changed_ratio": 1.0,
                "missing_selectors": [],
                "added_elements": [],
                "diff": None,
                "reason": f"missing_baseline:{page_key}:{baseline_label}",
            }

        before_dom = baseline["dom_summary"]
        diff = diff_snapshots(before_dom, current_dom_summary)

        before_elements = _extract_element_keys(before_dom)
        after_elements = _extract_element_keys(current_dom_summary)
        before_set = set(before_elements)
        after_set = set(after_elements)
        missing_selectors = [el for el in before_elements if el not in after_set]
        added_elements = [el for el in after_elements if el not in before_set]

        current_sha = _sha256_hex(_canonical_json(current_dom_summary))
        if current_sha == baseline["sha256"]:
            changed_ratio = 0.0
        else:
            before_text = str(before_dom.get("text") or "")
            after_text = str(current_dom_summary.get("text") or "")
            if before_text == after_text:
                text_ratio = 0.0
            else:
                text_ratio = 1.0 - difflib.SequenceMatcher(None, before_text, after_text).ratio()

            union_elements = before_set | after_set
            if union_elements:
                element_ratio = (len(missing_selectors) + len(added_elements)) / len(union_elements)
            else:
                element_ratio = 0.0

            meta_ratio = 0.0
            if diff["title_changed"]:
                meta_ratio += 0.2
            if diff["url_changed"]:
                meta_ratio += 0.2

            changed_ratio = round(min(1.0, max(text_ratio, element_ratio, meta_ratio)), 4)

        passed = changed_ratio <= max_changed_ratio
        return {
            "passed": passed,
            "has_baseline": True,
            "page_key": str(page_key),
            "baseline_snapshot_id": baseline["snapshot_id"],
            "baseline_sha256": baseline["sha256"],
            "current_sha256": current_sha,
            "baseline_label": baseline_label,
            "max_changed_ratio": max_changed_ratio,
            "changed_ratio": changed_ratio,
            "missing_selectors": missing_selectors,
            "added_elements": added_elements,
            "diff": diff,
            "reason": "ok" if passed else f"dom_regression_exceeded:{changed_ratio:.4f}>{max_changed_ratio:.4f}",
        }
