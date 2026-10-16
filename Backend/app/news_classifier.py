"""
news_classifier.py — Claude-based catalyst classifier (feature #9).

The existing ``compute_news_context`` in ``analyzer.py`` is a keyword
matcher: it looks for literals like "earnings" / "FDA" / "upgrade" in
headlines. That catches the easy cases but completely misses the
substance — "Judge blocks Activision acquisition" is a massive bearish
catalyst with zero matching keywords, while "Earnings season kicks off"
is a generic headline that trips every keyword.

This module asks Claude the trader's actual question for each batch of
articles attached to a symbol:

    "Ignoring chart patterns — are any of these headlines the kind of
     news that would make a day trader rethink a {direction} position
     in {symbol} right now?"

It returns a structured verdict with:

    - ``overall_impact``: bullish / bearish / mixed / none
    - ``severity``:       low / medium / high
    - ``is_catalyst``:    True when at least one article is material
    - ``articles``:       per-headline breakdown (impact + 1-liner)
    - ``label``:          one-line summary for the approval card

The autotrade scan loop then interprets the direction-agnostic verdict
against the actual trade direction:

    pro-thesis catalyst    -> 1.10  (small confirm)
    contradicting catalyst -> 0.50  (hard damp, typically kills the trade)
    contradicting + high   -> 0.20  (veto-tier — strongly suggests skipping)
    mixed / no catalyst    -> 1.00  (no effect, let the setup speak)

Why Haiku? This is a short classification — "does this headline flip
the thesis?" is a yes/no judgment, not a reasoning problem. Haiku
costs ~$0.003 per call and returns in <2s, so per-scan news cost stays
under $0.02/symbol even without caching. Sonnet-level reasoning + a
10k-token thinking budget (as analyze_chart does) would be overkill
and cost ~30× more for a strictly worse latency profile.

Degraded behavior: when Claude or the SDK is unavailable, we return
``None`` and the multiplier silently no-ops to 1.0 — same pattern as
the regime snapshot in #8. News absence should never nuke the book.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from typing import Literal

import anthropic

from .settings import settings

logger = logging.getLogger(__name__)


# ── Constants ────────────────────────────────────────────────────────────────

# Claude model. Haiku is fast + cheap + plenty smart for "is this a catalyst?".
# Sonnet is overkill and burns the rate limit budget we need for analyze_chart.
_MODEL = "claude-haiku-4-5"

# Max articles per classification. Alpaca returns up to 50 per fetch; we cap
# input to the model at 8 to keep the prompt small and the verdict focused on
# what's actually recent and relevant. More than 8 headlines usually means
# "industry news mentioning the ticker", not company-specific catalysts.
_MAX_ARTICLES = 8

# Max characters per article summary we send to the model. Prevents the prompt
# from ballooning if Alpaca returns a long-form analyst note.
_MAX_SUMMARY_CHARS = 400

# Multiplier table: overall_impact × severity × pro/contra-thesis.
# Rows are direction relative to the verdict:
#   "confirms"      = trade direction matches the catalyst bias
#   "contradicts"   = trade direction opposes the catalyst bias
#   "mixed"         = classifier returned mixed signals → small caution
#   "none"          = no material catalyst → full weight
#
# High-severity contradictions get the "veto" multiplier (0.20). At the default
# 0.80 gate, any pre-news confidence below 1.0 gets nuked below threshold. For
# confirming high-severity news we boost modestly (1.10) — the setup was
# already good enough to be here, we don't want news alone minting confidence.
_MULT_TABLE: dict[tuple[str, str], float] = {
    ("confirms",    "low"):    1.05,
    ("confirms",    "medium"): 1.10,
    ("confirms",    "high"):   1.10,
    ("contradicts", "low"):    0.85,
    ("contradicts", "medium"): 0.50,
    ("contradicts", "high"):   0.20,
    ("mixed",       "low"):    0.95,
    ("mixed",       "medium"): 0.90,
    ("mixed",       "high"):   0.80,
    ("none",        "low"):    1.00,
    ("none",        "medium"): 1.00,
    ("none",        "high"):   1.00,
}


Impact   = Literal["bullish", "bearish", "mixed", "none"]
Severity = Literal["low", "medium", "high"]


# ── Public types ─────────────────────────────────────────────────────────────

@dataclass
class ArticleVerdict:
    """Per-article classification."""
    headline: str
    source:   str | None
    impact:   Impact
    severity: Severity
    reason:   str                             # 1-line Claude-provided rationale


@dataclass
class NewsVerdict:
    """Aggregate catalyst verdict for a symbol."""
    symbol:         str
    articles_seen:  int                        # how many articles went to Claude
    is_catalyst:    bool                       # at least one material catalyst
    overall_impact: Impact                     # aggregated over the articles
    severity:       Severity                   # the worst severity in the batch
    label:          str                        # human-readable for the UI
    articles:       list[ArticleVerdict] = field(default_factory=list)
    articles_hash:  str = ""                   # stable id for caching


# ── Public API ───────────────────────────────────────────────────────────────

def classify_news(
    symbol: str,
    articles: list[dict] | None,
) -> NewsVerdict | None:
    """Classify a list of Alpaca news articles for the given symbol.

    Returns ``None`` when Claude is unavailable or returns nothing parseable —
    callers treat that as "no news signal, multiplier = 1.0". This is the
    same degraded-no-op pattern as regime detection (#8).
    """
    if not settings.anthropic_api_key:
        logger.debug("news_classify_skip symbol=%s reason=no_api_key", symbol)
        return None
    if not articles:
        return _empty_verdict(symbol, articles_hash=_hash_articles([]))

    # Filter to articles that explicitly mention this symbol. Alpaca's news
    # feed is lagged and broad — a single fetch for "NVDA" can include pieces
    # about AMD + TSMC that just mention NVDA in passing.
    sym_upper = symbol.upper()
    relevant = [
        a for a in articles
        if sym_upper in [s.upper() for s in (a.get("symbols") or [])]
    ] or articles  # fallback if no ticker-match; caller filtered
    relevant = relevant[:_MAX_ARTICLES]
    articles_hash = _hash_articles(relevant)

    prompt = _build_prompt(symbol, relevant)

    try:
        # 2026-05-19 hot-fix: hard 12s timeout at the HTTP layer (Anthropic
        # SDK default is 10 minutes ≈ infinity for our purposes). When the
        # API stalls under load, the threadpool worker has to STOP — three
        # autotrade hangs today coincided with stuck Haiku calls during
        # post-match processing. A timeout exception is caught below and
        # returned as None (degraded path), which is the documented contract.
        client = anthropic.Anthropic(
            api_key=settings.anthropic_api_key,
            timeout=12.0,
        )
        message = client.messages.create(
            model=_MODEL,
            max_tokens=1500,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = (message.content[0].text or "").strip()
    except Exception as exc:
        # Degraded mode: never crash the scan loop. Return None and the
        # autotrade caller silently no-ops the multiplier.
        logger.warning("news_classify_failed symbol=%s error=%s",
                       symbol, str(exc)[:200])
        return None

    try:
        verdict = _parse_verdict(
            raw, symbol=symbol, articles=relevant, articles_hash=articles_hash,
        )
    except Exception as exc:
        logger.warning("news_classify_parse_failed symbol=%s error=%s raw=%r",
                       symbol, exc, raw[:200])
        return None

    logger.info(
        "news_classify symbol=%s impact=%s sev=%s catalyst=%s n=%d",
        symbol, verdict.overall_impact, verdict.severity,
        verdict.is_catalyst, verdict.articles_seen,
    )
    return verdict


def news_multiplier(
    verdict: NewsVerdict | None,
    direction: str,
) -> tuple[float, dict]:
    """Interpret a verdict in the context of a specific trade direction.

    Returns ``(multiplier, metadata_dict)``. ``metadata_dict`` is a JSON-safe
    payload suitable for attaching to ``TradeLog`` so the approval card can
    render "Bearish catalyst (high severity) — 0.20× — Judge blocks merger".

    Direction-to-thesis mapping:
        long  + bullish  -> confirms
        long  + bearish  -> contradicts
        short + bullish  -> contradicts
        short + bearish  -> confirms
        (either) + mixed -> mixed
        (either) + none  -> none
    """
    if verdict is None or direction not in {"long", "short"}:
        return 1.0, {
            "impact":     None,
            "severity":   None,
            "multiplier": 1.0,
            "stance":     "none",
            "label":      "No news signal (classifier unavailable)" if verdict is None else "Direction unknown",
            "articles":   [],
        }

    if verdict.overall_impact == "none":
        stance = "none"
    elif verdict.overall_impact == "mixed":
        stance = "mixed"
    elif (direction == "long"  and verdict.overall_impact == "bullish") \
         or (direction == "short" and verdict.overall_impact == "bearish"):
        stance = "confirms"
    else:
        stance = "contradicts"

    mult = _MULT_TABLE.get((stance, verdict.severity), 1.0)

    return mult, {
        "impact":     verdict.overall_impact,
        "severity":   verdict.severity,
        "multiplier": round(mult, 3),
        "stance":     stance,
        "label":      verdict.label,
        "articles":   [
            {
                "headline": a.headline,
                "source":   a.source,
                "impact":   a.impact,
                "severity": a.severity,
                "reason":   a.reason,
            }
            for a in verdict.articles
        ],
    }


def verdict_dict(v: NewsVerdict | None) -> dict | None:
    """JSON-safe serialization for websockets / logs / UI."""
    if v is None:
        return None
    return {
        "symbol":          v.symbol,
        "articles_seen":   v.articles_seen,
        "is_catalyst":     v.is_catalyst,
        "overall_impact":  v.overall_impact,
        "severity":        v.severity,
        "label":           v.label,
        "articles_hash":   v.articles_hash,
        "articles":        [
            {
                "headline": a.headline,
                "source":   a.source,
                "impact":   a.impact,
                "severity": a.severity,
                "reason":   a.reason,
            }
            for a in v.articles
        ],
    }


# ── Internals ────────────────────────────────────────────────────────────────

_PROMPT = """You are classifying stock news for a day trader who is actively considering a trade in {symbol}.

For each headline below, decide:
1. **impact**: bullish (good for {symbol}) / bearish (bad for {symbol}) / mixed / none (cosmetic filler — routine press release, vague analyst chatter, other tickers incidentally mentioned)
2. **severity**: high (the kind of news that would flip a day-trader's thesis — earnings beat/miss, FDA decisions, M&A, guidance cuts, major lawsuits, executive departures) / medium (material but not thesis-breaking — analyst upgrades, contract wins, partnership announcements) / low (routine — 13F filings, blog posts, speculation)
3. **reason**: one tight sentence explaining your call

Then aggregate into an overall verdict:
- **overall_impact**: bullish / bearish / mixed / none
- **severity**: high / medium / low — take the WORST severity across catalyst articles (high wins over medium wins over low)
- **is_catalyst**: true if at least one article has medium or high severity and a non-none impact
- **label**: one short phrase like "Bearish catalyst: FDA rejection" or "No material catalysts"

Articles:
{articles_block}

Return ONLY valid JSON in exactly this shape, no prose, no markdown fence:

{{
  "overall_impact": "bullish" | "bearish" | "mixed" | "none",
  "severity": "high" | "medium" | "low",
  "is_catalyst": true | false,
  "label": "<short phrase>",
  "articles": [
    {{"index": 0, "impact": "bullish|bearish|mixed|none", "severity": "high|medium|low", "reason": "<one sentence>"}},
    ...
  ]
}}"""


def _build_prompt(symbol: str, articles: list[dict]) -> str:
    lines = []
    for i, art in enumerate(articles):
        headline = (art.get("headline") or "").strip()
        summary  = (art.get("summary")  or "").strip()[:_MAX_SUMMARY_CHARS]
        source   = (art.get("source")   or "").strip()
        when     = (art.get("created_at") or "").strip()
        parts = [f"[{i}] {headline}"]
        if source or when:
            parts.append(f"    ({source}{', ' if source and when else ''}{when})")
        if summary:
            parts.append(f"    {summary}")
        lines.append("\n".join(parts))
    articles_block = "\n\n".join(lines) if lines else "(no articles)"
    return _PROMPT.format(symbol=symbol, articles_block=articles_block)


def _parse_verdict(
    raw: str,
    *,
    symbol: str,
    articles: list[dict],
    articles_hash: str,
) -> NewsVerdict:
    # Strip markdown fences defensively — some models wrap JSON in ```json.
    text = raw.strip()
    if text.startswith("```"):
        parts = text.split("```")
        text = parts[1] if len(parts) > 1 else text
        text = text.lstrip("json").strip()

    parsed = json.loads(text)

    overall_impact = _coerce_impact(parsed.get("overall_impact"))
    severity       = _coerce_severity(parsed.get("severity"))
    is_catalyst    = bool(parsed.get("is_catalyst"))
    label          = str(parsed.get("label") or "").strip() \
                      or _default_label(overall_impact, severity)

    raw_articles = parsed.get("articles") or []
    per_article: list[ArticleVerdict] = []
    # Index back into the source list by the index field Claude echoed.
    for art_v in raw_articles:
        try:
            idx = int(art_v.get("index", -1))
        except (TypeError, ValueError):
            idx = -1
        if not (0 <= idx < len(articles)):
            continue
        src = articles[idx]
        per_article.append(ArticleVerdict(
            headline=(src.get("headline") or "").strip(),
            source=(src.get("source") or "").strip() or None,
            impact=_coerce_impact(art_v.get("impact")),
            severity=_coerce_severity(art_v.get("severity")),
            reason=str(art_v.get("reason") or "").strip()[:300],
        ))

    return NewsVerdict(
        symbol=symbol,
        articles_seen=len(articles),
        is_catalyst=is_catalyst,
        overall_impact=overall_impact,
        severity=severity,
        label=label,
        articles=per_article,
        articles_hash=articles_hash,
    )


def _coerce_impact(v) -> Impact:
    s = str(v or "none").strip().lower()
    return s if s in ("bullish", "bearish", "mixed", "none") else "none"  # type: ignore[return-value]


def _coerce_severity(v) -> Severity:
    s = str(v or "low").strip().lower()
    return s if s in ("low", "medium", "high") else "low"  # type: ignore[return-value]


def _default_label(impact: Impact, severity: Severity) -> str:
    if impact == "none":
        return "No material catalysts"
    return f"{impact.capitalize()} catalyst ({severity})"


def _empty_verdict(symbol: str, *, articles_hash: str) -> NewsVerdict:
    """Verdict for the "we fetched but Alpaca returned 0 articles" case."""
    return NewsVerdict(
        symbol=symbol,
        articles_seen=0,
        is_catalyst=False,
        overall_impact="none",
        severity="low",
        label="No recent news",
        articles=[],
        articles_hash=articles_hash,
    )


def _hash_articles(articles: list[dict]) -> str:
    """Stable hash of the article list for cache keying.

    Uses article IDs when present, headlines + timestamps as fallback. This
    lets the autotrade cache decide whether a fresh fetch is really new
    material or the same 8 articles as five minutes ago.
    """
    ident = []
    for a in articles:
        aid = a.get("id")
        if aid:
            ident.append(str(aid))
        else:
            ident.append(f"{a.get('headline','')}|{a.get('created_at','')}")
    digest = hashlib.sha256("\n".join(ident).encode("utf-8")).hexdigest()
    return digest[:16]
