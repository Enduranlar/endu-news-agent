"""LLM relevance scoring + categorization.

Pulls everything unscored (IG posts + web items), scores in batches against the
interest list (interests.yaml drives the prompt, so the operator tunes behaviour
by editing config — not code), and writes the verdict back. Items scored
relevant=false are kept in the DB (with relevant=0) but never surface in reports.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from .config_loader import Interests
from .llm import SCORE_BATCH_SIZE, LLMClient
from .store import Store

log = logging.getLogger("agent.relevance")


@dataclass
class RelevanceResult:
    scored: int = 0
    relevant: int = 0


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
    store: Store, llm: LLMClient, interests: Interests
) -> RelevanceResult:
    result = RelevanceResult()
    result_ig = _score_ig(store, llm, interests)
    result_web = _score_web(store, llm, interests)
    result.scored = result_ig.scored + result_web.scored
    result.relevant = result_ig.relevant + result_web.relevant
    log.info("Relevance: scored %d, relevant %d", result.scored, result.relevant)
    return result


def _score_ig(store: Store, llm: LLMClient, interests: Interests) -> RelevanceResult:
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
        scores = llm.score_items(items, interests)
        for row, score in zip(batch, scores):
            store.update_ig_score(
                row["post_id"],
                score.relevant,
                score.category,
                score.importance,
                score.one_line,
            )
            res.scored += 1
            if score.relevant:
                res.relevant += 1
    return res


def _score_web(store: Store, llm: LLMClient, interests: Interests) -> RelevanceResult:
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
        scores = llm.score_items(items, interests)
        for row, score in zip(batch, scores):
            store.update_web_score(
                row["item_id"],
                score.relevant,
                score.category,
                score.importance,
                score.one_line,
            )
            res.scored += 1
            if score.relevant:
                res.relevant += 1
    return res
