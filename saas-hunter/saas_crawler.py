"""
saas_miner.py — Advanced SaaS Opportunity Miner
================================================
Mines Reddit threads, GitHub issues, G2-style reviews, HN comments,
and web pages for real pain signals. Scores with a 4-tier engine,
deduplicates, and exports ranked CSV + JSON.

Install:
    pip install aiohttp praw beautifulsoup4 pandas rich fake-useragent

Run:
    python saas_miner.py
    python saas_miner.py --min-score 12 --output gold.csv
    python saas_miner.py --no-reddit --no-github   # web-only mode

Env vars (optional — enables richer sources):
    REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET  → real Reddit API access
    GITHUB_TOKEN                            → 5000 req/hr instead of 60
"""

import asyncio
import hashlib
import json
import os
import random
import re
import time
import argparse
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

import aiohttp
import pandas as pd
from bs4 import BeautifulSoup
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.progress import (BarColumn, MofNCompleteColumn, Progress,
                           SpinnerColumn, TaskProgressColumn, TextColumn,
                           TimeElapsedColumn, TimeRemainingColumn)
from rich.table import Table
from rich import box

try:
    from fake_useragent import UserAgent as _UA
    _ua_gen = _UA()
    def _ua(): return _ua_gen.random
except Exception:
    _AGENTS = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 Safari/605.1.15",
        "Mozilla/5.0 (X11; Linux x86_64) Gecko/20100101 Firefox/125.0",
    ]
    def _ua(): return random.choice(_AGENTS)


# ─────────────────────────────────────────────────────────────────────────────
# SOURCES
# ─────────────────────────────────────────────────────────────────────────────

# Web pages to scrape (articles, landing pages, blog posts)
WEB_TARGETS = [
    "https://news.ycombinator.com/",
    "https://www.indiehackers.com/",
    "https://www.producthunt.com/",
    "https://dev.to/",
    "https://lobste.rs/",
    "https://hackernoon.com/",
    "https://www.betalist.com/",
    "https://startupbase.io/",
    "https://huggingface.co/blog",
    "https://www.latent.space/",
    "https://www.deeplearning.ai/blog/",
    "https://techcrunch.com/startups/",
    "https://venturebeat.com/category/ai/",
    "https://thenextweb.com/startups",
    # HN "Ask HN" and "Is there a tool" searches
    "https://hn.algolia.com/?dateRange=pastMonth&page=0&prefix=false&query=is%20there%20a%20tool&sort=byPopularity&type=comment",
    "https://hn.algolia.com/?dateRange=pastMonth&page=0&prefix=false&query=wish%20someone%20would%20build&sort=byPopularity&type=comment",
    "https://hn.algolia.com/?dateRange=pastMonth&page=0&prefix=false&query=manually%20every%20week&sort=byPopularity&type=comment",
]

# Reddit subreddits to scrape (public JSON API — no auth needed)
REDDIT_SUBREDDITS = [
    "startups", "SaaS", "Entrepreneur", "smallbusiness",
    "productivity", "nocode", "webdev", "devops",
    "sales", "marketing", "freelance", "consulting",
    "accounting", "humanresources", "projectmanagement",
]

# GitHub repos to mine issues from (owner/repo)
GITHUB_REPOS = [
    # Workflow / automation OSS — their enhancement issues = unbuilt SaaS features
    "n8n-io/n8n", "huginn/huginn", "activepieces/activepieces",
    # Data / BI
    "metabase/metabase", "apache/superset",
    # CRM / sales
    "twentyhq/twenty", "mautic/mautic",
    # Dev productivity
    "linear-app/linear", "makeplane/plane",
    # AI tooling
    "langchain-ai/langchain", "run-llama/llama_index",
]


# ─────────────────────────────────────────────────────────────────────────────
# SCORING ENGINE
# ─────────────────────────────────────────────────────────────────────────────

# Tier 1 — Direct pain statements (highest signal)
TIER_1 = {
    "no tool for": 12,          "nothing exists that": 12,
    "can't find anything": 12,  "nobody has built": 12,
    "wish someone would build": 11,
    "we do this manually": 12,  "still doing it by hand": 12,
    "manually every": 11,       "copy paste every": 11,
    "takes me hours to": 10,    "takes hours every": 11,
    "looking for a tool": 11,   "does anyone know a tool": 11,
    "any recommendations for": 10,
    "is there a way to automate": 12,
    "we still use spreadsheets": 12,
    "i wish there was": 10,     "wish there was a": 10,
    "can't believe there's no": 11,
    "paying someone to manually": 12,
    "hire someone to": 10,
}

# Tier 2 — Workflow pain combos
TIER_2 = {
    "manual process": 8,        "manual workflow": 8,
    "spreadsheet for": 8,       "spreadsheet to track": 9,
    "excel for": 7,             "google sheets for": 8,
    "no integration": 8,        "doesn't integrate": 8,
    "copy paste between": 9,    "export then import": 8,
    "download then upload": 8,  "no api": 7,
    "can't automate": 9,        "manually checking": 8,
    "manually updating": 8,     "manually entering": 8,
    "takes too long": 7,        "time consuming": 6,
    "repetitive task": 8,       "tedious to": 7,
    "painful to": 7,            "nightmare to": 9,
    "so frustrating": 8,        "kills productivity": 9,
    "broken workflow": 9,       "missing feature": 7,
    "feature request": 6,       "would love if": 7,
    "really wish": 8,
}

# Tier 3 — Frequency amplifiers (score also boosts nearby tier 1/2 hits)
TIER_3_FREQ = {
    "every day": 5,    "every week": 5,   "daily": 4,
    "weekly": 4,       "each month": 5,   "every client": 6,
    "each new": 5,     "every sprint": 5, "every quarter": 4,
    "each time": 4,    "whenever we": 3,  "every single": 5,
    "recurring": 4,    "repeatedly": 4,
}

# Tier 4 — Business domain anchors (give context, increase relevance)
TIER_4_DOMAIN = {
    # Finance
    "invoice": 5,           "reconciliation": 5,    "payroll": 5,
    "accounts payable": 5,  "expense report": 5,    "billing": 4,
    "financial report": 4,  "bookkeeping": 4,
    # Sales / CRM
    "sales pipeline": 4,    "lead scoring": 4,      "crm": 3,
    "follow up": 3,         "outreach": 3,           "prospecting": 4,
    "cold email": 4,        "sales process": 4,
    # HR / People
    "onboarding": 4,        "offboarding": 4,        "performance review": 4,
    "interview scheduling": 4, "employee tracking": 4,
    # Customer Success
    "churn": 5,             "retention": 4,          "nps": 3,
    "customer health": 4,   "usage tracking": 4,     "support ticket": 4,
    # DevOps
    "deployment": 3,        "release notes": 4,      "incident report": 4,
    "on-call": 3,           "monitoring": 3,
    # Marketing
    "content calendar": 4,  "social scheduling": 4,  "reporting": 3,
    "attribution": 4,       "campaign tracking": 4,
    # General business
    "client report": 5,     "status update": 4,      "project tracking": 4,
    "approval workflow": 5, "data entry": 6,          "data migration": 5,
}

# Negation phrases — reduce score when pain is already solved
NEGATIONS = [
    "we automated", "already integrated", "tool that handles",
    "we use .{0,20} for this", "solved by", "fixed with",
    "there's a great tool", "highly recommend",
    "works perfectly", "no longer an issue",
]
_neg_pattern = re.compile(
    "|".join(NEGATIONS), re.IGNORECASE
)

# Source quality multipliers — some sources are worth more
SOURCE_MULTIPLIERS = {
    "reddit_comment":   2.5,
    "reddit_post":      2.0,
    "github_issue":     2.0,
    "github_discussion":1.8,
    "hn_comment":       2.5,
    "web_article":      0.9,
    "product_hunt":     1.0,
}

# Flatten all keyword tiers into one lookup
KEYWORD_MAP: dict[str, int] = {}
for _d in [TIER_1, TIER_2, TIER_3_FREQ, TIER_4_DOMAIN]:
    KEYWORD_MAP.update(_d)

_kw_pattern = re.compile(
    r"(" + "|".join(re.escape(k) for k in sorted(KEYWORD_MAP, key=len, reverse=True)) + r")",
    re.IGNORECASE,
)


def score_text(text: str, source_type: str = "web_article") -> tuple[int, list[str]]:
    """Score a snippet. Returns (final_score, matched_keywords)."""
    t = text.lower()

    # Negation check — halve score if pain is already solved
    neg_penalty = 0.5 if _neg_pattern.search(t) else 1.0

    matched: dict[str, int] = {}
    has_freq = False
    for m in _kw_pattern.finditer(t):
        kw = m.group(0).lower()
        matched.setdefault(kw, KEYWORD_MAP.get(kw, 1))
        if kw in TIER_3_FREQ:
            has_freq = True

    t1 = sum(v for k, v in matched.items() if k in TIER_1)
    t2 = sum(v for k, v in matched.items() if k in TIER_2)
    t3 = sum(v for k, v in matched.items() if k in TIER_3_FREQ)
    t4 = sum(v for k, v in matched.items() if k in TIER_4_DOMAIN)

    freq_boost = 1.5 if has_freq and (t1 + t2) > 0 else 1.0
    raw = (t1 + t2) * freq_boost + t3 + t4
    final = raw * neg_penalty * SOURCE_MULTIPLIERS.get(source_type, 1.0)

    return int(final), list(matched.keys())


# ─────────────────────────────────────────────────────────────────────────────
# DATA MODEL
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Opportunity:
    text: str
    score: int
    matched_keywords: list[str]
    source: str
    source_type: str
    domain: str
    crawled_at: str
    text_hash: str = field(init=False)

    def __post_init__(self):
        self.text_hash = hashlib.md5(self.text.encode()).hexdigest()

    def to_dict(self) -> dict:
        d = asdict(self)
        d["matched_keywords"] = ", ".join(self.matched_keywords)
        return d


# ─────────────────────────────────────────────────────────────────────────────
# RATE LIMITER
# ─────────────────────────────────────────────────────────────────────────────

class RateLimiter:
    def __init__(self, min_delay: float = 1.5):
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._last:  dict[str, float]        = defaultdict(float)
        self.min_delay = min_delay

    async def acquire(self, domain: str):
        async with self._locks[domain]:
            wait = self.min_delay - (time.monotonic() - self._last[domain])
            if wait > 0:
                await asyncio.sleep(wait + random.uniform(0, 0.4))
            self._last[domain] = time.monotonic()


# ─────────────────────────────────────────────────────────────────────────────
# SHARED STATE
# ─────────────────────────────────────────────────────────────────────────────

class MinerState:
    """Thread-safe shared state for all crawlers."""
    def __init__(self, min_score: int):
        self.min_score = min_score
        self.results: list[Opportunity] = []
        self.seen_hashes: set[str] = set()
        self.stats = defaultdict(int)  # crawled, failed, opportunities, duplicates
        self._lock = asyncio.Lock()

    async def add(self, opp: Opportunity) -> bool:
        """Add opportunity if it passes score threshold and isn't a duplicate."""
        async with self._lock:
            if opp.score < self.min_score:
                return False
            if opp.text_hash in self.seen_hashes:
                self.stats["duplicates"] += 1
                return False
            self.seen_hashes.add(opp.text_hash)
            self.results.append(opp)
            self.stats["opportunities"] += 1
            return True

    async def record(self, key: str, n: int = 1):
        async with self._lock:
            self.stats[key] += n


# ─────────────────────────────────────────────────────────────────────────────
# WEB CRAWLER
# ─────────────────────────────────────────────────────────────────────────────

class WebCrawler:
    """Async crawler for generic web pages and articles."""

    def __init__(self, state: MinerState, rate_limiter: RateLimiter,
                 max_concurrency: int = 10, timeout: int = 12, max_retries: int = 2):
        self.state = state
        self.rl = rate_limiter
        self.semaphore = asyncio.Semaphore(max_concurrency)
        self.timeout = timeout
        self.max_retries = max_retries

    def _headers(self):
        return {
            "User-Agent": _ua(),
            "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
        }

    async def _fetch(self, session: aiohttp.ClientSession, url: str) -> Optional[str]:
        domain = urlparse(url).netloc
        await self.rl.acquire(domain)
        for attempt in range(self.max_retries + 1):
            try:
                async with session.get(
                    url, headers=self._headers(),
                    timeout=aiohttp.ClientTimeout(total=self.timeout),
                    ssl=False, allow_redirects=True,
                ) as r:
                    if r.status == 200:
                        ct = r.headers.get("content-type", "")
                        if "html" not in ct:
                            return None
                        return await r.text(errors="replace")
                    elif r.status in (429, 503):
                        await asyncio.sleep(5 * (attempt + 1))
                    else:
                        return None
            except (asyncio.TimeoutError, aiohttp.ClientError):
                if attempt < self.max_retries:
                    await asyncio.sleep(2 ** attempt)
            except Exception:
                break
        return None

    def _extract(self, html: str) -> list[str]:
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
            tag.decompose()
        texts = []
        for tag in soup.find_all(["h1", "h2", "h3", "p", "li", "blockquote"]):
            t = re.sub(r"\s+", " ", tag.get_text(separator=" ", strip=True))
            if len(t) >= 40:
                texts.append(t)
        return texts

    async def crawl_one(self, session: aiohttp.ClientSession, url: str,
                        progress: Progress, task_id):
        async with self.semaphore:
            html = await self._fetch(session, url)
            domain = urlparse(url).netloc
            await self.state.record("web_crawled")
            if not html:
                await self.state.record("web_failed")
                progress.advance(task_id)
                return

            now = datetime.now(timezone.utc).isoformat()
            for text in self._extract(html):
                s, kws = score_text(text, "web_article")
                await self.state.add(Opportunity(
                    text=text, score=s, matched_keywords=kws,
                    source=url, source_type="web_article",
                    domain=domain, crawled_at=now,
                ))
            progress.advance(task_id)

    async def run(self, urls: list[str], progress: Progress, task_id):
        connector = aiohttp.TCPConnector(limit=30, ssl=False, ttl_dns_cache=300)
        async with aiohttp.ClientSession(connector=connector) as session:
            await asyncio.gather(*[
                self.crawl_one(session, url, progress, task_id) for url in urls
            ])


# ─────────────────────────────────────────────────────────────────────────────
# REDDIT MINER (public JSON API — no auth required)
# ─────────────────────────────────────────────────────────────────────────────

class RedditMiner:
    """
    Mines Reddit using the public .json API endpoint.
    No API key needed. Fetches top + new posts and their comment threads.
    Set REDDIT_CLIENT_ID + REDDIT_CLIENT_SECRET env vars for OAuth (more data).
    """

    BASE = "https://www.reddit.com"
    SORTS = ["top", "new", "hot"]
    MIN_COMMENT_LEN = 60

    def __init__(self, state: MinerState, rate_limiter: RateLimiter):
        self.state = state
        self.rl = rate_limiter

    def _headers(self):
        return {"User-Agent": "saas-miner/2.0 (research tool)"}

    async def _get(self, session: aiohttp.ClientSession, url: str) -> Optional[dict]:
        await self.rl.acquire("www.reddit.com")
        try:
            async with session.get(
                url, headers=self._headers(),
                timeout=aiohttp.ClientTimeout(total=15), ssl=False,
            ) as r:
                if r.status == 200:
                    return await r.json()
                elif r.status == 429:
                    await asyncio.sleep(10)
        except Exception:
            pass
        return None

    def _extract_comments(self, data, depth=0) -> list[str]:
        """Recursively extract all comment bodies from a Reddit comment tree."""
        texts = []
        if not isinstance(data, dict):
            return texts
        kind = data.get("kind")
        d = data.get("data", {})
        if kind == "Listing":
            for child in d.get("children", []):
                texts.extend(self._extract_comments(child, depth))
        elif kind == "t1":  # comment
            body = d.get("body", "")
            if len(body) >= self.MIN_COMMENT_LEN and body != "[deleted]":
                texts.append(body)
            replies = d.get("replies")
            if isinstance(replies, dict) and depth < 3:
                texts.extend(self._extract_comments(replies, depth + 1))
        return texts

    async def mine_subreddit(self, session: aiohttp.ClientSession,
                             subreddit: str, progress: Progress, task_id):
        await self.state.record("reddit_subs")
        now = datetime.now(timezone.utc).isoformat()

        for sort in self.SORTS:
            url = f"{self.BASE}/r/{subreddit}/{sort}.json?limit=25&t=month"
            data = await self._get(session, url)
            if not data:
                continue

            posts = data.get("data", {}).get("children", [])
            for post in posts:
                pd_ = post.get("data", {})
                title = pd_.get("title", "")
                selftext = pd_.get("selftext", "")
                post_url = pd_.get("url", "")
                permalink = pd_.get("permalink", "")

                # Score the post title + body together
                combined = f"{title}. {selftext}"
                if len(combined) >= 40:
                    s, kws = score_text(combined, "reddit_post")
                    await self.state.add(Opportunity(
                        text=combined[:500], score=s, matched_keywords=kws,
                        source=f"{self.BASE}{permalink}",
                        source_type="reddit_post",
                        domain=f"reddit.com/r/{subreddit}",
                        crawled_at=now,
                    ))

                # Mine comments for this post
                comments_url = f"{self.BASE}{permalink}.json?limit=50&depth=3"
                comments_data = await self._get(session, comments_url)
                if comments_data and isinstance(comments_data, list) and len(comments_data) > 1:
                    comment_texts = self._extract_comments(comments_data[1])
                    for text in comment_texts:
                        s, kws = score_text(text, "reddit_comment")
                        await self.state.add(Opportunity(
                            text=text[:500], score=s, matched_keywords=kws,
                            source=f"{self.BASE}{permalink}",
                            source_type="reddit_comment",
                            domain=f"reddit.com/r/{subreddit}",
                            crawled_at=now,
                        ))

        progress.advance(task_id)

    async def run(self, subreddits: list[str], progress: Progress, task_id):
        connector = aiohttp.TCPConnector(limit=5, ssl=False)
        async with aiohttp.ClientSession(connector=connector) as session:
            # Reddit is strict on rate limits — run sequentially with delays
            for sub in subreddits:
                await self.mine_subreddit(session, sub, progress, task_id)
                await asyncio.sleep(random.uniform(1.5, 3.0))


# ─────────────────────────────────────────────────────────────────────────────
# GITHUB ISSUE MINER
# ─────────────────────────────────────────────────────────────────────────────

class GitHubMiner:
    """
    Mines GitHub issues labelled enhancement/feature-request.
    Sorts by most upvotes (👍) — highly upvoted open issues = validated unbuilt demand.
    Set GITHUB_TOKEN env var for 5000 req/hr instead of 60.
    """

    API = "https://api.github.com"
    ISSUE_LABELS = ["enhancement", "feature-request", "feature request",
                    "help wanted", "good first issue", "roadmap"]
    MIN_UPVOTES = 3

    def __init__(self, state: MinerState):
        self.state = state
        token = os.environ.get("GITHUB_TOKEN", "")
        self.headers = {
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "saas-miner/2.0",
        }
        if token:
            self.headers["Authorization"] = f"Bearer {token}"

    async def _get(self, session: aiohttp.ClientSession, url: str,
                   params: dict = None) -> Optional[list | dict]:
        try:
            async with session.get(
                url, headers=self.headers, params=params,
                timeout=aiohttp.ClientTimeout(total=15), ssl=False,
            ) as r:
                if r.status == 200:
                    return await r.json()
                elif r.status == 403:
                    # Rate limited
                    retry_after = int(r.headers.get("Retry-After", 60))
                    await asyncio.sleep(min(retry_after, 30))
        except Exception:
            pass
        return None

    async def mine_repo(self, session: aiohttp.ClientSession,
                        repo: str, progress: Progress, task_id):
        await self.state.record("github_repos")
        now = datetime.now(timezone.utc).isoformat()

        data = await self._get(session, f"{self.API}/repos/{repo}/issues", {
            "state": "open",
            "sort": "reactions",
            "direction": "desc",
            "per_page": 50,
        })
        if not data or not isinstance(data, list):
            progress.advance(task_id)
            return

        for issue in data:
            upvotes = issue.get("reactions", {}).get("+1", 0)
            if upvotes < self.MIN_UPVOTES:
                continue

            title = issue.get("title", "")
            body = (issue.get("body") or "")[:600]
            labels = [l["name"] for l in issue.get("labels", [])]
            days_open = (datetime.now(timezone.utc) -
                         datetime.fromisoformat(
                             issue["created_at"].replace("Z", "+00:00")
                         )).days
            url = issue.get("html_url", f"https://github.com/{repo}/issues")

            # Build a rich text representation for scoring
            text = (
                f"{title}. {body} "
                f"[{upvotes} upvotes, open {days_open} days, labels: {', '.join(labels)}]"
            )
            s, kws = score_text(text, "github_issue")

            # Boost score based on signal strength:
            # High upvotes on old open issues = maintainer won't fix = SaaS gap
            bonus = 0
            if upvotes >= 20:  bonus += 8
            elif upvotes >= 10: bonus += 4
            if days_open >= 180: bonus += 5
            elif days_open >= 90: bonus += 3

            await self.state.add(Opportunity(
                text=text[:500], score=s + bonus, matched_keywords=kws,
                source=url, source_type="github_issue",
                domain=f"github.com/{repo}",
                crawled_at=now,
            ))

        progress.advance(task_id)

    async def run(self, repos: list[str], progress: Progress, task_id):
        connector = aiohttp.TCPConnector(limit=10, ssl=False)
        async with aiohttp.ClientSession(connector=connector) as session:
            await asyncio.gather(*[
                self.mine_repo(session, repo, progress, task_id)
                for repo in repos
            ])


# ─────────────────────────────────────────────────────────────────────────────
# HN ALGOLIA COMMENT MINER
# ─────────────────────────────────────────────────────────────────────────────

class HNMiner:
    """
    Mines Hacker News comments via the Algolia HN API (free, no auth).
    Searches for high-signal phrases directly — much better than scraping articles.
    """

    API = "https://hn.algolia.com/api/v1"

    # Pain phrases to search for directly in HN comments
    SEARCH_QUERIES = [
        "is there a tool that",
        "wish someone would build",
        "we do this manually",
        "no good tool for",
        "anyone know a tool",
        "spreadsheet to track",
        "manually every week",
        "takes hours every",
        "copy paste between",
        "no integration between",
        "still using excel for",
        "wish there was a saas",
        "looking for something that",
        "does anyone automate",
        "painful workflow",
    ]

    def __init__(self, state: MinerState):
        self.state = state

    async def _search(self, session: aiohttp.ClientSession, query: str) -> list[dict]:
        try:
            async with session.get(
                f"{self.API}/search",
                params={
                    "query": query,
                    "tags": "comment",
                    "hitsPerPage": 30,
                    "numericFilters": "points>1",  # some signal of quality
                },
                timeout=aiohttp.ClientTimeout(total=15),
                ssl=False,
            ) as r:
                if r.status == 200:
                    data = await r.json()
                    return data.get("hits", [])
        except Exception:
            pass
        return []

    async def mine_query(self, session: aiohttp.ClientSession,
                         query: str, progress: Progress, task_id):
        hits = await self._search(session, query)
        now = datetime.now(timezone.utc).isoformat()

        for hit in hits:
            text = hit.get("comment_text") or hit.get("story_text") or ""
            # Strip HTML tags
            text = re.sub(r"<[^>]+>", " ", text)
            text = re.sub(r"\s+", " ", text).strip()
            if len(text) < 40:
                continue

            url = f"https://news.ycombinator.com/item?id={hit.get('objectID', '')}"
            s, kws = score_text(text, "hn_comment")
            await self.state.add(Opportunity(
                text=text[:500], score=s, matched_keywords=kws,
                source=url, source_type="hn_comment",
                domain="news.ycombinator.com",
                crawled_at=now,
            ))

        progress.advance(task_id)
        await asyncio.sleep(0.5)  # gentle pacing

    async def run(self, progress: Progress, task_id):
        connector = aiohttp.TCPConnector(limit=5, ssl=False)
        async with aiohttp.ClientSession(connector=connector) as session:
            for q in self.SEARCH_QUERIES:
                await self.mine_query(session, q, progress, task_id)


# ─────────────────────────────────────────────────────────────────────────────
# RICH UI
# ─────────────────────────────────────────────────────────────────────────────

BANNER = """[bold cyan]
 ███████╗ █████╗  █████╗ ███████╗    ███╗   ███╗██╗███╗   ██╗███████╗██████╗
 ██╔════╝██╔══██╗██╔══██╗██╔════╝    ████╗ ████║██║████╗  ██║██╔════╝██╔══██╗
 ███████╗███████║███████║███████╗    ██╔████╔██║██║██╔██╗ ██║█████╗  ██████╔╝
 ╚════██║██╔══██║██╔══██║╚════██║    ██║╚██╔╝██║██║██║╚██╗██║██╔══╝  ██╔══██╗
 ███████║██║  ██║██║  ██║███████║    ██║ ╚═╝ ██║██║██║ ╚████║███████╗██║  ██║
 ╚══════╝╚═╝  ╚═╝╚═╝  ╚═╝╚══════╝   ╚═╝     ╚═╝╚═╝╚═╝  ╚═══╝╚══════╝╚═╝  ╚═╝
[/bold cyan][dim]         Reddit · GitHub Issues · HN Comments · Web  //  async pain signal miner[/dim]
"""

SCORE_STYLE = {
    range(7, 12):   ("yellow",     "●"),
    range(12, 20):  ("orange1",    "●●"),
    range(20, 35):  ("bright_red", "●●●"),
    range(35, 999): ("bold bright_red", "●●●●"),
}

def _score_fmt(s: int) -> tuple[str, str]:
    for r, (color, dots) in SCORE_STYLE.items():
        if s in r:
            return color, dots
    return "white", "○"

def build_table(results: list[Opportunity], limit: int = 20) -> Table:
    t = Table(
        title=f"[bold]Top {limit} Opportunities[/bold]",
        box=box.MINIMAL_HEAVY_HEAD,
        show_lines=True,
        border_style="cyan",
        header_style="bold cyan",
        min_width=110,
    )
    t.add_column("Score",   justify="center", width=8)
    t.add_column("Signal",  justify="center", width=6)
    t.add_column("Type",    width=14)
    t.add_column("Domain",  width=22)
    t.add_column("Snippet", width=55)
    t.add_column("Keywords", width=25)

    top = sorted(results, key=lambda x: x.score, reverse=True)[:limit]
    for o in top:
        color, dots = _score_fmt(o.score)
        preview = o.text[:120] + "…" if len(o.text) > 120 else o.text
        t.add_row(
            f"[{color}][bold]{o.score}[/bold][/{color}]",
            f"[{color}]{dots}[/{color}]",
            f"[dim]{o.source_type}[/dim]",
            o.domain,
            preview,
            ", ".join(o.matched_keywords[:4]),
        )
    return t

def build_stats(stats: dict, elapsed: float) -> Panel:
    grid = Table.grid(padding=(0, 3))
    grid.add_column(justify="right", style="dim")
    grid.add_column(justify="left")

    rate = (stats.get("web_crawled", 0) + stats.get("reddit_subs", 0)) / max(elapsed, 1)
    grid.add_row("Web pages",        f"[green]{stats.get('web_crawled', 0)}[/green]")
    grid.add_row("Reddit subs",      f"[green]{stats.get('reddit_subs', 0)}[/green]")
    grid.add_row("GitHub repos",     f"[green]{stats.get('github_repos', 0)}[/green]")
    grid.add_row("HN searches",      f"[green]{len(HNMiner.SEARCH_QUERIES)}[/green]")
    grid.add_row("Opportunities",    f"[bold yellow]{stats.get('opportunities', 0)}[/bold yellow]")
    grid.add_row("Duplicates dropped", f"[dim]{stats.get('duplicates', 0)}[/dim]")
    grid.add_row("Throughput",       f"[cyan]{rate:.1f}[/cyan] sources/sec")
    grid.add_row("Elapsed",          f"[white]{elapsed:.1f}s[/white]")

    return Panel(grid, title="[bold]Stats[/bold]", border_style="green", padding=(1, 2))

def build_source_breakdown(results: list[Opportunity]) -> Table:
    counts = defaultdict(lambda: {"count": 0, "max_score": 0, "total_score": 0})
    for o in results:
        counts[o.source_type]["count"] += 1
        counts[o.source_type]["max_score"] = max(counts[o.source_type]["max_score"], o.score)
        counts[o.source_type]["total_score"] += o.score

    t = Table(title="Results by Source Type", box=box.SIMPLE_HEAVY,
              border_style="dim", header_style="bold")
    t.add_column("Source Type")
    t.add_column("Opportunities", justify="right")
    t.add_column("Avg Score",     justify="right")
    t.add_column("Max Score",     justify="right")

    for stype, d in sorted(counts.items(), key=lambda x: x[1]["count"], reverse=True):
        avg = d["total_score"] / max(d["count"], 1)
        t.add_row(stype, str(d["count"]), f"{avg:.1f}",
                  f"[bold yellow]{d['max_score']}[/bold yellow]")
    return t


# ─────────────────────────────────────────────────────────────────────────────
# EXPORT
# ─────────────────────────────────────────────────────────────────────────────

def export(results: list[Opportunity], path: str) -> tuple[pd.DataFrame, str]:
    records = [o.to_dict() for o in results]
    df = pd.DataFrame(records).sort_values("score", ascending=False).reset_index(drop=True)
    df.to_csv(path, index=False)
    json_path = path.rsplit(".", 1)[0] + ".json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(records, f, indent=2, default=str)
    return df, json_path


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(
        description="Advanced SaaS Opportunity Miner",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--min-score",       type=int,   default=10,
                   help="Minimum composite score to record a snippet")
    p.add_argument("--output",          type=str,   default="saas_opportunities.csv",
                   help="CSV output path (JSON written alongside it)")
    p.add_argument("--top",             type=int,   default=20,
                   help="Top N results to display in terminal")
    p.add_argument("--concurrency",     type=int,   default=8,
                   help="Max concurrent web requests")
    p.add_argument("--rate-delay",      type=float, default=1.5,
                   help="Per-domain rate limit delay (seconds)")
    p.add_argument("--no-reddit",       action="store_true",
                   help="Skip Reddit mining")
    p.add_argument("--no-github",       action="store_true",
                   help="Skip GitHub issue mining")
    p.add_argument("--no-hn",           action="store_true",
                   help="Skip Hacker News comment mining")
    p.add_argument("--no-web",          action="store_true",
                   help="Skip web page crawling")
    return p.parse_args()


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

async def main():
    args = parse_args()
    console = Console()
    console.print(BANNER)

    state = MinerState(min_score=args.min_score)
    rl = RateLimiter(min_delay=args.rate_delay)

    # Calculate total work units for the progress bar
    total = 0
    if not args.no_web:    total += len(WEB_TARGETS)
    if not args.no_reddit: total += len(REDDIT_SUBREDDITS)
    if not args.no_github: total += len(GITHUB_REPOS)
    if not args.no_hn:     total += len(HNMiner.SEARCH_QUERIES)

    # Config summary
    sources = []
    if not args.no_web:    sources.append(f"[cyan]{len(WEB_TARGETS)}[/cyan] web pages")
    if not args.no_reddit: sources.append(f"[cyan]{len(REDDIT_SUBREDDITS)}[/cyan] subreddits")
    if not args.no_github: sources.append(f"[cyan]{len(GITHUB_REPOS)}[/cyan] GitHub repos")
    if not args.no_hn:     sources.append(f"[cyan]{len(HNMiner.SEARCH_QUERIES)}[/cyan] HN searches")
    console.print(Panel(
        "  ·  ".join(sources) + f"\n[dim]min score: {args.min_score}  ·  concurrency: {args.concurrency}[/dim]",
        title="Sources", border_style="dim",
    ))
    console.print()

    progress = Progress(
        SpinnerColumn(),
        TextColumn("[bold cyan]{task.description}"),
        BarColumn(bar_width=38, style="cyan", complete_style="bright_cyan"),
        TaskProgressColumn(), MofNCompleteColumn(),
        TimeElapsedColumn(), TimeRemainingColumn(),
        console=console,
    )

    start = time.monotonic()
    task_id = progress.add_task("Mining…", total=total)

    with Live(progress, console=console, refresh_per_second=10):
        tasks = []

        if not args.no_web:
            web = WebCrawler(state, rl, max_concurrency=args.concurrency)
            tasks.append(web.run(WEB_TARGETS, progress, task_id))

        if not args.no_reddit:
            reddit = RedditMiner(state, rl)
            tasks.append(reddit.run(REDDIT_SUBREDDITS, progress, task_id))

        if not args.no_github:
            github = GitHubMiner(state)
            tasks.append(github.run(GITHUB_REPOS, progress, task_id))

        if not args.no_hn:
            hn = HNMiner(state)
            tasks.append(hn.run(progress, task_id))

        await asyncio.gather(*tasks)

    elapsed = time.monotonic() - start

    # ── Results ───────────────────────────────────────────────────────────────
    console.print()
    console.print(build_stats(state.stats, elapsed))
    console.print()

    if state.results:
        console.print(build_table(state.results, limit=args.top))
        console.print()
        console.print(build_source_breakdown(state.results))
        console.print()

    # ── Export ────────────────────────────────────────────────────────────────
    df, json_path = export(state.results, args.output)
    console.print(Panel(
        f"[bold green]✓[/bold green]  CSV  →  [cyan]{args.output}[/cyan]\n"
        f"[bold green]✓[/bold green]  JSON →  [cyan]{json_path}[/cyan]\n\n"
        f"[dim]{len(df)} opportunities sorted by score descending[/dim]",
        title="[bold]Saved[/bold]", border_style="green",
    ))
    console.print(f"\n[bold green]Done in {elapsed:.1f}s.[/bold green]\n")


if __name__ == "__main__":
    asyncio.run(main())
