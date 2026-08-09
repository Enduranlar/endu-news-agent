"""LLM relevance scoring + categorization (+ long-term memory).

Pulls everything unscored (IG posts + web items), scores in batches against the
interest list (interests.yaml drives the prompt, so the operator tunes behaviour
by editing config — not code), and writes the verdict back. Items scored
relevant=false are kept in the DB (with relevant=0) but never surface in reports.

Memory (config/memory.yaml, optional): before scoring a batch we recall only the
memory entries whose subject tokens appear in that batch — an index lookup, not a
full dump — and pass them along. The model then flags items that merely repeat an
already-reported fact (they're stored relevant=0, so recurring "registration is
open" posts stop showing up in every report) and names any new fact worth
remembering, which we write back.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from .config_loader import Interests, MemoryConfig
from .llm import SCORE_BATCH_SIZE, LLMClient, ScoreResult
from .store import Store

log = logging.getLogger("agent.relevance")


@dataclass
class RelevanceResult:
    scored: int = 0
    relevant: int = 0
    repeats: int = 0          # suppressed as already-reported
    remembered: int = 0       # new memory entries written


def _chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i : i + n]


def _ts_to_date(ts) -> str:
    if not ts:
        return ""
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d")
    except (ValueError, OSError, OverflowError):
        return ""


def score_pending(
    store: Store,
    llm: LLMClient,
    interests: Interests,
    memory: Optional[MemoryConfig] = None,
) -> RelevanceResult:
    result = RelevanceResult()
    for part in (
        _score_ig(store, llm, interests, memory),
        _score_web(store, llm, interests, memory),
    ):
        result.scored += part.scored
        result.relevant += part.relevant
        result.repeats += part.repeats
        result.remembered += part.remembered
    log.info(
        "Relevance: scored %d, relevant %d, repeats suppressed %d, remembered %d",
        result.scored,
        result.relevant,
        result.repeats,
        result.remembered,
    )
    return result


# --- Memory helpers ----------------------------------------------------------


def _recall_for_batch(
    store: Store, memory: Optional[MemoryConfig], items: list[dict]
) -> list[dict]:
    """Index lookup: memory entries matching this batch's text (capped)."""
    if not (memory and memory.enabled):
        return []
    blob = " ".join(f"{it.get('title','')} {it.get('text','')}" for it in items)
    rows = store.recall(blob, limit=memory.max_entries_in_prompt)
    return [
        {
            "topic": r["topic"],
            "subject": r["subject"],
            "fact": r["fact"],
            "date": (r["first_seen_at"] or "")[:10],
        }
        for r in rows
    ]


def _apply_memory(
    store: Store,
    memory: Optional[MemoryConfig],
    score: ScoreResult,
    url: str,
    res: RelevanceResult,
) -> tuple[bool, str]:
    """Handle one scored item's memory outcome.

    Returns (relevant, score_reason). The reason keeps the three ways an item can
    end up relevant=0 distinguishable — model judgment, memory suppression, or a
    failed call — so cross-agent analysis measures judgment rather than dedup or
    errors."""
    relevant = score.relevant
    reason = "error" if score.failed else "model"
    if not (memory and memory.enabled):
        return relevant, reason

    topic = memory.topic(score.memory_topic) if score.memory_topic else None

    if score.repeat and (topic is None or topic.suppress_repeats):
        relevant = False
        if reason != "error":
            reason = "memory_repeat"
        res.repeats += 1

    if topic and score.memory_subject and score.one_line:
        try:
            if store.remember(
                topic=topic.id,
                subject=score.memory_subject,
                fact=score.one_line,
                source_url=url,
                ttl_days=topic.ttl_days or memory.default_ttl_days,
            ):
                res.remembered += 1
        except Exception as exc:  # noqa: BLE001 — memory must never break scoring
            log.warning("remember failed (%s)", exc)
    return relevant, reason


# --- Scoring -----------------------------------------------------------------


def _score_ig(
    store: Store,
    llm: LLMClient,
    interests: Interests,
    memory: Optional[MemoryConfig] = None,
) -> RelevanceResult:
    res = RelevanceResult()
    rows = store.unscored_ig_posts()
    for batch in _chunks(rows, SCORE_BATCH_SIZE):
        items = [
            {
                "source": f"Instagram @{r['handle']}",
                "title": "",
                "text": r["caption"] or "",
                "url": r["url"],
                "date": _ts_to_date(r["ts"]),
            }
            for r in batch
        ]
        recalled = _recall_for_batch(store, memory, items)
        scores = llm.score_items(items, interests, memory, recalled)
        for row, score in zip(batch, scores):
            relevant, reason = _apply_memory(store, memory, score, row["url"], res)
            store.update_ig_score(
                row["post_id"],
                relevant,
                score.category,
                score.importance,
                score.one_line,
                score_reason=reason,
            )
            res.scored += 1
            if relevant:
                res.relevant += 1
    return res


def _score_web(
    store: Store,
    llm: LLMClient,
    interests: Interests,
    memory: Optional[MemoryConfig] = None,
) -> RelevanceResult:
    res = RelevanceResult()
    rows = store.unscored_web_items()
    for batch in _chunks(rows, SCORE_BATCH_SIZE):
        items = [
            {
                "source": "Web",
                "title": r["title"] or "",
                "text": r["text_excerpt"] or "",
                "url": r["url"],
                "date": r["published_at"] or "",
            }
            for r in batch
        ]
        recalled = _recall_for_batch(store, memory, items)
        scores = llm.score_items(items, interests, memory, recalled)
        for row, score in zip(batch, scores):
            relevant, reason = _apply_memory(store, memory, score, row["url"], res)
            store.update_web_score(
                row["item_id"],
                relevant,
                score.category,
                score.importance,
                score.one_line,
                score_reason=reason,
            )
            res.scored += 1
            if relevant:
                res.relevant += 1
    return res
