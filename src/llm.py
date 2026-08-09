"""LLM wrapper: relevance scoring, memory, source vetting, summary writing.

Two providers behind one interface — OpenRouter (recommended: many models via one
key, and it returns the real per-call cost, which we log for the cost report) and
the direct Anthropic API. Each agent in the fleet gets its own client, so several
models can run the same pipeline side by side.

Two models per the brief: a cheap/fast filter model (default claude-haiku-4-5)
for per-item relevance + categorization and source vetting, and a stronger model
(default claude-sonnet-4-6) for the twice-weekly summary. LLM traffic goes
DIRECT — never through the Turkish proxy.

Relevance + vetting use structured JSON output (output_config.format) so parsing
is reliable; the summary is free-form Markdown text.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Optional

import anthropic
import httpx

from .config_loader import Interests, MemoryConfig

log = logging.getLogger("agent.llm")

# Keep batches small enough that a single failure doesn't waste a huge call,
# but large enough to amortize the shared interest-list prompt prefix.
SCORE_BATCH_SIZE = 8


@dataclass
class ScoreResult:
    relevant: bool
    category: Optional[str]
    importance: int
    one_line: str
    # Memory fields (populated only when memory is enabled in config/memory.yaml).
    repeat: bool = False           # already reported — suppress
    memory_topic: str = ""         # topic id to remember this under ("" = nothing)
    memory_subject: str = ""       # subject the fact is about


@dataclass
class VetResult:
    on_topic: bool
    reason: str
    credibility: str


@dataclass
class ProbeResult:
    """Outcome of a cheap end-to-end health check for one agent."""

    ok: bool = False
    structured_ok: bool = False
    text_ok: bool = False
    latency_s: float = 0.0
    error: str = ""


_PROBE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {"ok": {"type": "boolean"}, "word": {"type": "string"}},
    "required": ["ok", "word"],
}


@dataclass
class RaceResultsExtract:
    found: bool
    summary: str  # Turkish, <=140 chars, naming top finishers if present


def _score_schema(
    category_ids: list[str], memory_topic_ids: Optional[list[str]] = None
) -> dict[str, Any]:
    """Scoring schema. When memory is enabled, each result also carries the
    repeat flag and the memory topic/subject to record."""
    props: dict[str, Any] = {
        "index": {"type": "integer"},
        "relevant": {"type": "boolean"},
        "category": {"type": "string", "enum": category_ids},
        "importance": {"type": "integer", "enum": [1, 2, 3, 4, 5]},
        "one_line": {"type": "string"},
    }
    required = ["index", "relevant", "category", "importance", "one_line"]
    if memory_topic_ids:
        props["repeat"] = {"type": "boolean"}
        props["memory_topic"] = {"type": "string", "enum": [*memory_topic_ids, ""]}
        props["memory_subject"] = {"type": "string"}
        required += ["repeat", "memory_topic", "memory_subject"]
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "results": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": props,
                    "required": required,
                },
            }
        },
        "required": ["results"],
    }


_VET_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "on_topic": {"type": "boolean"},
        "reason": {"type": "string"},
        "credibility": {"type": "string"},
    },
    "required": ["on_topic", "reason", "credibility"],
}

_RESULTS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "found": {"type": "boolean"},
        "summary": {"type": "string"},
    },
    "required": ["found", "summary"],
}

_DEDUPE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "groups": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "indices": {"type": "array", "items": {"type": "integer"}}
                },
                "required": ["indices"],
            },
        }
    },
    "required": ["groups"],
}


class LLMClient:
    """Model client for one agent.

    provider="openrouter" (recommended) uses OpenAI-compatible chat completions:
    many models under one key, and the response carries the real cost, which we
    log. provider="anthropic" uses the direct API — tokens are logged but cost
    stays 0 (the API doesn't return a price).

    Both go DIRECT — never through the Turkish proxy. When `store` is given, every
    call is recorded to llm_usage for the cost report.
    """

    def __init__(self, api_key: str, filter_model: str, summary_model: str,
                 provider: str = "anthropic", base_url: str = "",
                 store: Any = None, agent: str = ""):
        self.provider = provider or "anthropic"
        self.filter_model = filter_model
        self.summary_model = summary_model
        self.store = store
        self.agent = agent
        self.client = None
        self._http = None
        if self.provider == "openrouter":
            self._http = httpx.Client(
                base_url=base_url or "https://openrouter.ai/api/v1",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                    "X-Title": "endu-news-agent",
                },
                timeout=httpx.Timeout(connect=10.0, read=180.0, write=30.0, pool=10.0),
            )
        else:
            # SDK auto-retries 429/5xx with backoff.
            self.client = anthropic.Anthropic(api_key=api_key, max_retries=3)

    def close(self) -> None:
        if self._http is not None:
            self._http.close()

    # --- Provider plumbing ---------------------------------------------

    def _record(self, call_type: str, model: str, usage: dict) -> None:
        if self.store is None:
            return
        try:
            self.store.record_llm_usage(
                agent=self.agent, model=model, call_type=call_type,
                prompt_tokens=int(usage.get("prompt_tokens") or 0),
                completion_tokens=int(usage.get("completion_tokens") or 0),
                cost_usd=float(usage.get("cost") or 0.0),
            )
        except Exception as exc:  # noqa: BLE001 — accounting never breaks a run
            log.warning("failed to record llm usage (%s)", exc)

    def _openrouter_call(self, model: str, prompt: str, max_tokens: int,
                         schema: Optional[dict]) -> tuple[str, dict]:
        body: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "usage": {"include": True},   # ask for the real cost of this call
        }
        if schema is not None:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "result", "strict": True, "schema": schema},
            }
        last: Optional[Exception] = None
        for attempt in range(4):
            try:
                resp = self._http.post("/chat/completions", json=body)
            except httpx.HTTPError as exc:
                last = exc
                time.sleep(min(30.0, 1.5 * (2**attempt)))
                continue
            if resp.status_code == 200:
                data = resp.json()
                if data.get("error"):
                    raise RuntimeError(f"OpenRouter error: {data['error']}")
                return (data["choices"][0]["message"]["content"] or ""), (
                    data.get("usage") or {}
                )
            if resp.status_code in (429, 500, 502, 503, 504):
                last = RuntimeError(f"{resp.status_code}: {resp.text[:200]}")
                ra = resp.headers.get("retry-after")
                try:
                    delay = float(ra) if ra else 1.5 * (2**attempt)
                except ValueError:
                    delay = 1.5 * (2**attempt)
                time.sleep(min(30.0, delay))
                continue
            raise RuntimeError(f"OpenRouter {resp.status_code}: {resp.text[:300]}")
        raise RuntimeError(f"OpenRouter request failed after retries: {last}")

    def _complete_json(self, prompt: str, schema: dict, max_tokens: int,
                       call_type: str, model: Optional[str] = None) -> dict:
        model = model or self.filter_model
        if self.provider == "openrouter":
            text, usage = self._openrouter_call(model, prompt, max_tokens, schema)
            self._record(call_type, model, usage)
            return self._parse_json(text)
        resp = self.client.messages.create(
            model=model, max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
            output_config={"format": {"type": "json_schema", "schema": schema}},
        )
        self._record(call_type, model, self._anthropic_usage(resp))
        return self._parse_json(self._extract_text(resp))

    def _complete_text(self, prompt: str, max_tokens: int, call_type: str,
                       model: Optional[str] = None) -> str:
        model = model or self.summary_model
        if self.provider == "openrouter":
            text, usage = self._openrouter_call(model, prompt, max_tokens, None)
            self._record(call_type, model, usage)
            return text
        resp = self.client.messages.create(
            model=model, max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        self._record(call_type, model, self._anthropic_usage(resp))
        return self._extract_text(resp)

    @staticmethod
    def _anthropic_usage(resp: Any) -> dict:
        u = getattr(resp, "usage", None)
        return {
            "prompt_tokens": getattr(u, "input_tokens", 0) or 0,
            "completion_tokens": getattr(u, "output_tokens", 0) or 0,
            "cost": 0.0,   # the Anthropic API doesn't return a price
        }

    # --- Relevance scoring ---------------------------------------------

    def score_items(
        self,
        items: list[dict[str, str]],
        interests: Interests,
        memory: Optional["MemoryConfig"] = None,
        recalled: Optional[list[dict[str, str]]] = None,
    ) -> list[ScoreResult]:
        """Score a batch of items for relevance + category.

        Each item is a dict with: source, title, text, url, date.
        Returns results aligned by position to `items`. On a hard failure the
        whole batch is treated as not-relevant (logged), so a flaky call never
        crashes a run.

        When `memory` is enabled, `recalled` carries only the memory entries that
        the index matched for this batch (topic/subject/fact/date). The model uses
        them to flag repeats and to say what new facts should be remembered.
        """
        if not items:
            return []

        mem_on = bool(memory and memory.enabled)
        schema = _score_schema(
            interests.category_ids, memory.topic_ids if mem_on else None
        )
        cat_lines = "\n".join(
            f"  - {c.id}: {c.label}" for c in interests.categories
        )
        items_block = "\n\n".join(
            f"[{i}] source: {it.get('source','')}\n"
            f"title: {it.get('title','')}\n"
            f"date: {it.get('date','')}\n"
            f"url: {it.get('url','')}\n"
            f"text: {self._truncate(it.get('text',''), 1200)}"
            for i, it in enumerate(items)
        )
        prompt = (
            "Sen bir dayanıklılık sporları beslenmesi haber ajansının alaka "
            "filtresisin.\n\nİŞ BAĞLAMI:\n"
            f"{interests.context}\n\n"
            "KATEGORİLER (alakalı öğeler için en uygun tek kategoriyi seç):\n"
            f"{cat_lines}\n\n"
            "GÖREV: Aşağıdaki numaralandırılmış her öğe için, iş bağlamıyla "
            "alakalı olup olmadığına ve kategorilerden birine uyup uymadığına "
            "karar ver. Her öğe için şunları döndür: index, relevant (bool), "
            "category (yukarıdaki id'lerden biri — relevant false olsa bile en "
            "yakınını seç), importance (1=önemsiz, 5=çok önemli) ve one_line "
            "(<=140 karakter, tarafsız bir özet). Reklamlar, çekilişler, genel "
            "motivasyon paylaşımları ve konu dışı her şey için relevant=false "
            "işaretle. Kaynak içeriği İngilizce olsa bile one_line alanını "
            "TÜRKÇE yaz.\n\n"
            f"{self._memory_block(memory, recalled) if mem_on else ''}"
            "ÖĞELER:\n"
            f"{items_block}\n\n"
            "Her öğe için aynı index ile bir sonuç nesnesi döndür."
        )

        try:
            data = self._complete_json(prompt, schema, 2000, "score")
            return self._align_results(data, len(items), interests)
        except Exception as exc:  # noqa: BLE001 — degrade gracefully, never crash a run
            log.warning("score_items batch failed (%s); marking batch not-relevant", exc)
            return [
                ScoreResult(False, None, 1, "") for _ in items
            ]

    @staticmethod
    def _memory_block(
        memory: "MemoryConfig", recalled: Optional[list[dict[str, str]]]
    ) -> str:
        """Compact memory section: what to remember + what we already reported.

        Only the entries the index matched for this batch are included, so this
        stays a few short lines (often empty)."""
        topic_lines = "\n".join(
            f"  - {t.id}: {t.label}"
            + (f" — {' '.join(t.description.split())}" if t.description else "")
            for t in memory.topics
        )
        known = ""
        if recalled:
            known_lines = "\n".join(
                f"  - [{e.get('topic','')}] {e.get('subject','')}: "
                f"{e.get('fact','')} ({e.get('date','')})"
                for e in recalled
            )
            known = (
                "DAHA ÖNCE RAPOR EDİLENLER (hafıza — bu konular operatöre zaten "
                "bildirildi):\n"
                f"{known_lines}\n\n"
                "Bir öğe yukarıdaki kayıtlardan biriyle AYNI konudaki aynı "
                "gelişmeyi tekrar ediyorsa repeat=true yap (o öğe rapora "
                "girmeyecek). Aynı konuda GERÇEKTEN YENİ bir gelişme varsa "
                "(ör. kayıtlar açıldı -> yarış tamamlandı) repeat=false yap.\n\n"
            )
        return (
            f"{known}"
            "HAFIZA KONULARI (kalıcı olarak hatırlanacak bilgi türleri):\n"
            f"{topic_lines}\n\n"
            "Her öğe için ayrıca şunları döndür: repeat (bool — yukarıdaki "
            "hafıza kayıtlarının tekrarı mı), memory_topic (öğe bu konulardan "
            "birine ait yeni bir bilgi taşıyorsa o konunun id'si, yoksa boş "
            "string) ve memory_subject (bilginin konusu; ör. yarışın tam adı — "
            "aynı konu için hep AYNI ismi kullan, yoksa boş string).\n\n"
        )

    def _align_results(
        self, data: dict, n: int, interests: Interests
    ) -> list[ScoreResult]:
        by_index: dict[int, ScoreResult] = {}
        for r in data.get("results", []):
            try:
                idx = int(r["index"])
            except (KeyError, ValueError, TypeError):
                continue
            cat = r.get("category")
            if cat not in interests.category_ids:
                cat = None
            imp = r.get("importance", 1)
            try:
                imp = max(1, min(5, int(imp)))
            except (ValueError, TypeError):
                imp = 1
            by_index[idx] = ScoreResult(
                relevant=bool(r.get("relevant", False)),
                category=cat,
                importance=imp,
                one_line=self._truncate(str(r.get("one_line", "")), 140),
                repeat=bool(r.get("repeat", False)),
                memory_topic=str(r.get("memory_topic", "") or "").strip(),
                memory_subject=self._truncate(
                    str(r.get("memory_subject", "") or "").strip(), 120
                ),
            )
        # Fill any missing index with a safe default.
        return [by_index.get(i, ScoreResult(False, None, 1, "")) for i in range(n)]

    # --- Source vetting (discovery) ------------------------------------

    def vet_source(
        self, descriptor: str, interests: Interests
    ) -> Optional[VetResult]:
        """Ask whether a candidate source is credible + on-topic.

        `descriptor` is a short blurb describing the candidate (handle + bio +
        follower count, or domain + homepage title/description). Returns None on
        a hard failure (caller skips the candidate)."""
        prompt = (
            "Bir dayanıklılık sporları beslenmesi işletmesi için aday haber "
            "kaynaklarını değerlendiriyorsun.\n\nİŞ BAĞLAMI:\n"
            f"{interests.context}\n\n"
            "ADAY KAYNAK:\n"
            f"{self._truncate(descriptor, 1500)}\n\n"
            "Karar ver: bu, dayanıklılık sporları / yarış / sporcu beslenmesi "
            "haberleri için güvenilir ve konuyla ilgili, operatörün dikkatine "
            "değer bir kaynak mı? Şunları döndür: on_topic (bool), reason (tek "
            "cümle, <=160 karakter, TÜRKÇE) ve credibility (ne kadar yetkili "
            "göründüğüne dair kısa bir not, TÜRKÇE)."
        )
        try:
            data = self._complete_json(prompt, _VET_SCHEMA, 500, "vet")
            return VetResult(
                on_topic=bool(data.get("on_topic", False)),
                reason=self._truncate(str(data.get("reason", "")), 200),
                credibility=self._truncate(str(data.get("credibility", "")), 200),
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("vet_source failed (%s); skipping candidate", exc)
            return None

    # --- Report-wide de-duplication ------------------------------------

    def find_duplicate_groups(
        self, items: list[dict[str, Any]]
    ) -> list[list[int]]:
        """Cluster report items that describe the SAME story/event.

        `items` is a list of dicts with: index, category, source, one_line.
        Returns a list of groups, each a list of item indices that are duplicates
        of one another (only groups of 2+). Returns [] on any failure so the report
        never fails over dedup."""
        if len(items) < 2:
            return []
        lines = "\n".join(
            f"[{it['index']}] (kategori: {it.get('category','')} | "
            f"kaynak: {it.get('source','')}) {it.get('one_line','')}"
            for it in items
        )
        prompt = (
            "Aşağıda bir haber raporundaki maddeler numaralandırılmış olarak var "
            "(kategori ve kaynak dahil). Bazı maddeler AYNI haberi/olayı farklı "
            "kaynaklardan tekrar ediyor olabilir. Aynı haberi/olayı anlatan madde "
            "gruplarını bul ve her grubun index'lerini döndür.\n\n"
            "Kurallar:\n"
            "- Sadece GERÇEKTEN aynı olayı/haberi anlatan maddeleri grupla (ör. "
            "aynı yarış sonucu, aynı rekor, aynı ürün lansmanı farklı kaynaklardan).\n"
            "- Sadece benzer konu ya da aynı yarış ama FARKLI bilgi içeren "
            "maddeleri gruplama.\n"
            "- Kategoriler farklı olsa bile aynı haberi anlatan maddeleri "
            "gruplayabilirsin (rapor genelinde çalış).\n"
            "- Tek maddelik grup döndürme; sadece 2+ maddelik gerçek tekrarları döndür.\n\n"
            f"MADDELER:\n{lines}\n\n"
            "Tekrar eden madde gruplarının index listelerini döndür."
        )
        try:
            data = self._complete_json(prompt, _DEDUPE_SCHEMA, 1500, "dedupe")
            out: list[list[int]] = []
            for g in data.get("groups", []):
                idxs = [int(i) for i in g.get("indices", []) if isinstance(i, int)]
                if len(idxs) >= 2:
                    out.append(idxs)
            return out
        except Exception as exc:  # noqa: BLE001 — never fail a report over dedup
            log.warning("find_duplicate_groups failed (%s); keeping all items", exc)
            return []

    # --- Health check ---------------------------------------------------

    def probe(self) -> ProbeResult:
        """Verify this agent's models actually work, for a fraction of a cent.

        Exercises exactly what the pipeline depends on: a **structured** call on
        the filter model (JSON schema — relevance, memory, vetting and race
        results all need it) and a short **text** call on the summary model when
        it differs. Errors are captured, never raised."""
        res = ProbeResult()
        started = time.monotonic()
        try:
            data = self._complete_json(
                "Reply with ok=true and word=\"pong\".",
                _PROBE_SCHEMA, 64, "healthcheck",
            )
            res.structured_ok = isinstance(data.get("ok"), bool)
            if not res.structured_ok:
                res.error = (
                    "returned JSON that doesn't match the schema: "
                    f"{str(data)[:120]}"
                )
        except json.JSONDecodeError:
            # Reached the model but got prose back — the usual sign that this
            # model can't honour a JSON schema, which the pipeline requires.
            res.error = (
                "did not return valid JSON — this model probably doesn't support "
                "structured outputs (required for scoring/memory/vetting)"
            )
            res.latency_s = time.monotonic() - started
            return res
        except Exception as exc:  # noqa: BLE001 — report, don't raise
            res.error = f"{type(exc).__name__}: {exc}"[:300]
            res.latency_s = time.monotonic() - started
            return res

        if self.summary_model and self.summary_model != self.filter_model:
            try:
                text = self._complete_text("Say: pong", 32, "healthcheck")
                res.text_ok = bool(text.strip())
                if not res.text_ok:
                    res.error = "summary model returned empty text"
            except Exception as exc:  # noqa: BLE001
                res.error = f"summary model — {type(exc).__name__}: {exc}"[:300]
                res.latency_s = time.monotonic() - started
                return res
        else:
            res.text_ok = True  # same model, already proven above

        res.latency_s = time.monotonic() - started
        res.ok = res.structured_ok and res.text_ok
        return res

    # --- Race results extraction ---------------------------------------

    def extract_race_results(
        self, race_name: str, page_text: str
    ) -> Optional[RaceResultsExtract]:
        """Extract finishing results from a race page's text.

        Returns found=False when the page has no results yet (race just finished,
        results not posted, or page is a generic event page). Returns None on a
        hard LLM failure (caller retries on a later run within the window)."""
        prompt = (
            "Aşağıda bir koşu/dayanıklılık yarışının web sayfasından alınan metin "
            "var. Bu metinde YARIŞ SONUÇLARI (bitiren sporcular / dereceye girenler) "
            "var mı? Varsa, ana kategori için ilk 3'ü (mümkünse kadın+erkek) içeren "
            "kısa, <=140 karakter TÜRKÇE bir özet yaz; isim ve varsa derece/süre ekle. "
            "Sonuç yoksa found=false döndür ve summary'yi boş bırak. Kayıt, program, "
            "parkur tanıtımı gibi içerikleri sonuç sayma.\n\n"
            f"YARIŞ: {race_name}\n\nSAYFA METNİ:\n{self._truncate(page_text, 6000)}"
        )
        try:
            data = self._complete_json(prompt, _RESULTS_SCHEMA, 400, "race_results")
            return RaceResultsExtract(
                found=bool(data.get("found", False)),
                summary=self._truncate(str(data.get("summary", "")), 140),
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("extract_race_results failed for %s (%s)", race_name, exc)
            return None

    # --- Summary writing -----------------------------------------------

    def write_summary(self, prompt: str) -> str:
        """Generate the twice-weekly brief with the stronger model. Returns Markdown."""
        return self._complete_text(prompt, 4000, "summary").strip()

    # --- Helpers --------------------------------------------------------

    @staticmethod
    def _truncate(text: str, limit: int) -> str:
        text = (text or "").strip()
        return text if len(text) <= limit else text[: limit - 1] + "…"

    @staticmethod
    def _extract_text(resp: Any) -> str:
        return "".join(
            block.text for block in resp.content if getattr(block, "type", "") == "text"
        )

    def _parse_json(self, text: str) -> dict:
        text = (text or "").strip()
        # output_config.format guarantees the first text block is valid JSON, but
        # be defensive: strip code fences if a model ever wraps it.
        if text.startswith("```"):
            text = text.strip("`")
            if text.lstrip().startswith("json"):
                text = text.lstrip()[4:]
        return json.loads(text)
