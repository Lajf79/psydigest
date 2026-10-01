#!/usr/bin/env python3
"""
PsyDigest Serverless — GitHub Actions edition.

Runs as a scheduled GitHub Actions workflow (or anywhere with Python 3.11+):
  PubMed (E-utilities) -> relevance scoring -> Mistral LLM summaries ->
  static site (docs/index.html + docs/data.json + docs/feed.xml).

No server, no database: state lives in docs/data.json committed to the repo.

Usage:
    python digest.py          # run one digest cycle (writes into docs/)
    python digest.py --check  # build site from existing data.json only (no fetch)

Secrets (environment variables): MISTRAL_API_KEY (required for LLM summaries),
NCBI_API_KEY (optional, raises PubMed rate limit). Without MISTRAL_API_KEY the
digest still works and stores abstract excerpts instead.

Dependencies: requests, pyyaml (see requirements.txt)
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from xml.etree import ElementTree
from xml.sax.saxutils import escape as xml_escape

import requests

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.environ.get("PSYDIGEST_CONFIG", os.path.join(HERE, "config.yaml"))
DOCS_DIR = os.path.join(HERE, "docs")

DEFAULT_CONFIG: dict[str, Any] = {
    "pubmed": {
        "email": "",                # polite: set your address (optional)
        "api_key": "",              # optional; 3 -> 10 req/s
        "days_back": 3,            # overlap so missed runs leave no gaps
        "initial_backfill_days": 14,  # used when data.json is empty
        "max_records_per_query": 100,
        "guideline_days_back": 30,
    },
    "llm": {
        "api_key": "",              # better: env MISTRAL_API_KEY
        "model": "mistral-small-latest",
        "min_score_for_summary": 6,
        "max_summaries_per_run": 25,
    },
    "site": {
        "title": "PsyDigest",
        "subtitle": "Daily psychiatry literature digest",
        # Set this to your GitHub Pages URL in config.yaml (used in RSS)
        "base_url": "",
        "retention_days": 90,       # articles kept in data.json
        "rss_limit": 50,
    },
    "queries": [
        {
            "name": "Key psychiatry journals (all content)",
            "term": (
                '("World Psychiatry"[ta] OR "Lancet Psychiatry"[ta] OR '
                '"JAMA Psychiatry"[ta] OR "Am J Psychiatry"[ta] OR '
                '"Mol Psychiatry"[ta] OR "Biol Psychiatry"[ta] OR '
                '"Br J Psychiatry"[ta] OR "Acta Psychiatr Scand"[ta] OR '
                '"Eur Neuropsychopharmacol"[ta] OR "J Clin Psychiatry"[ta] OR '
                '"Psychol Med"[ta] OR "Schizophr Bull"[ta] OR '
                '"Transl Psychiatry"[ta] OR "Gen Psychiatry"[ta] OR '
                '"BMC Psychiatry"[ta] OR "Nord J Psychiatry"[ta])'
            ),
        },
        {
            "name": "Psychosis spectrum",
            "term": '("Schizophrenia"[MeSH] OR "Psychotic Disorders"[MeSH])',
        },
        {
            "name": "Affective disorders",
            "term": (
                '("Depressive Disorder"[MeSH] OR "Bipolar Disorder"[MeSH]) '
                'AND (treatment OR therapy OR trial OR antidepressant* OR psychotherap* '
                'OR prophyla* OR maintenance OR relapse OR prevention OR guideline*)'
            ),
        },
        {
            "name": "Anxiety, OCD, trauma",
            "term": (
                '("Anxiety Disorders"[MeSH] OR "Obsessive-Compulsive Disorder"[MeSH] '
                'OR "Stress Disorders, Post-Traumatic"[MeSH]) '
                'AND (treatment OR therapy OR trial OR psychotherap* OR prevention)'
            ),
        },
        {
            "name": "ADHD and autism",
            "term": (
                '("Attention Deficit Hyperactivity Disorder"[MeSH] '
                'OR "Autism Spectrum Disorder"[MeSH] OR ADHD[tiab] OR autistic[tiab]) '
                'AND (treatment OR therapy OR trial OR medication OR guideline*)'
            ),
        },
        {
            "name": "Substance use disorders",
            "term": (
                '"Substance-Related Disorders"[MeSH] '
                'AND (treatment OR therapy OR trial OR prevention OR guideline*)'
            ),
        },
        {
            "name": "Eating disorders",
            "term": '"Eating Disorders"[MeSH]',
        },
        {
            "name": "Suicide and self-harm",
            "term": (
                '("Suicide"[MeSH] OR "Self-Injurious Behavior"[MeSH]) '
                'AND (risk OR prevention OR prediction OR intervention OR screening)'
            ),
        },
        {
            "name": "Guidelines & recommendations",
            "term": (
                '("Practice Guideline"[pt] OR guideline*[ti]) '
                'AND (psychiatr*[tiab] OR "Mental Disorders"[MeSH] '
                'OR "mental health"[tiab] OR antipsychotic*[tiab] '
                'OR antidepressant*[tiab] OR suicide[tiab])'
            ),
            "category": "Guidelines",
        },
    ],
    "priority_journals": [
        "world psychiatry", "lancet psychiatry", "jama psychiatry", "am j psychiatry",
        "mol psychiatry", "biol psychiatry", "br j psychiatry", "acta Psychiatr Scand".lower(),
        "eur neuropsychopharmacol", "j clin psychiatry", "psychol med",
        "schizophr bull", "transl psychiatry", "gen psychiatry", "nord j psychiatry",
    ],
    "scoring": {
        "high_value": [
            "meta-analysis", "systematic review", "network meta-analysis",
            "randomized", "randomised", "pragmatic trial", "practice guideline",
            "guideline", "consensus statement", "clinical practice",
            "comparative effectiveness", "effectiveness",
        ],
        "moderate_value": [
            "antipsychotic", "antidepressant", "mood stabili", "psychotherapy",
            "cognitive behavioural", "cognitive behavioral", "cbt", "ketamine",
            "esketamine", "clozapine", "lithium", "aripiprazole", "olanzapine",
            "quetiapine", "ssri", "snri", "tolerability", "side effect",
            "adherence", "discontinuation", "relapse", "maintenance treatment",
            "prevention", "suicide risk", "screening", "prediction model",
            "mortality", "cardiometabolic", "long-acting injectable",
        ],
        "journal_bonus": 4,
        "guideline_bonus": 6,
    },
    "watch_links": [
        {
            "name": "NICE — guidelines and newsletters",
            "url": "https://www.nice.org.uk/guidance",
            "note": "Filter: Mental health and wellbeing. InDepth alerts: "
                    "https://www.nice.org.uk/nice-newsletters-and-alerts",
        },
        {
            "name": "Socialstyrelsen — kunskapsstöd och nyhetsbrev",
            "url": "https://www.socialstyrelsen.se/kunskapsstod-och-regler/",
            "note": "Nyhetsbrev: https://www.socialstyrelsen.se/om-socialstyrelsen/nyhetsbrev/",
        },
        {
            "name": "WFSBP — biological psychiatry guidelines",
            "url": "https://www.wfsbp.org/guidelines/",
        },
        {
            "name": "Läkemedelsverket — behandlingsrekommendationer",
            "url": "https://www.lakemedelsverket.se/sv/behandling-rekommendationer/",
        },
    ],
}


def deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config() -> dict:
    cfg = DEFAULT_CONFIG
    if os.path.exists(CONFIG_PATH):
        import yaml
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            cfg = deep_merge(cfg, yaml.safe_load(fh) or {})
    if os.environ.get("MISTRAL_API_KEY"):
        cfg["llm"]["api_key"] = os.environ["MISTRAL_API_KEY"]
    if os.environ.get("NCBI_API_KEY"):
        cfg["pubmed"]["api_key"] = os.environ["NCBI_API_KEY"]
    return cfg


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger("psydigest")

# ---------------------------------------------------------------------------
# PubMed client (E-utilities) — same logic as the self-hosted edition
# ---------------------------------------------------------------------------

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"


class PubMedClient:
    def __init__(self, cfg: dict):
        p = cfg["pubmed"]
        self.email = p.get("email", "")
        self.api_key = p.get("api_key", "")
        self.min_interval = 0.38 if self.api_key else (1.0 / 3.0) * 1.15
        self._last_call = 0.0
        self.session = requests.Session()

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_call
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        self._last_call = time.monotonic()

    def _params(self, extra: dict) -> dict:
        params = {"tool": "psydigest", "retmode": "json"}
        if self.email:
            params["email"] = self.email
        if self.api_key:
            params["api_key"] = self.api_key
        params.update(extra)
        return params

    def _get(self, path: str, params: dict) -> requests.Response:
        self._throttle()
        resp = self.session.get(f"{EUTILS}/{path}", params=params, timeout=60)
        resp.raise_for_status()
        return resp

    def search(self, term: str, days_back: int, max_records: int) -> list[str]:
        resp = self._get(
            "esearch.fcgi",
            self._params(
                {
                    "db": "pubmed",
                    "term": term,
                    "datetype": "edat",
                    "reldate": days_back,
                    "retmax": max_records,
                    "sort": "pub_date",
                }
            ),
        )
        result = resp.json().get("esearchresult", {})
        if result.get("error"):
            raise RuntimeError(f"PubMed esearch error: {result['error']}")
        return list(result.get("idlist", []))

    def fetch_details(self, pmids: list[str]) -> list[dict]:
        articles: list[dict] = []
        for i in range(0, len(pmids), 100):
            chunk = pmids[i : i + 100]
            resp = self._get(
                "efetch.fcgi",
                self._params(
                    {
                        "db": "pubmed",
                        "id": ",".join(chunk),
                        "retmode": "xml",
                        "rettype": "abstract",
                    }
                ),
            )
            articles.extend(parse_pubmed_xml(resp.content))
        return articles


_INLINE_TAGS = re.compile(r"</?(?:i|b|u|em|strong|sub|sup)>")


def _strip_tags(el: Optional[ElementTree.Element]) -> str:
    if el is None:
        return ""
    text = "".join(el.itertext())
    text = _INLINE_TAGS.sub("", text)
    return re.sub(r"\s+", " ", text).strip()


def parse_pubmed_xml(raw: bytes) -> list[dict]:
    """Parse a PubmedArticleSet XML payload into plain article dicts."""
    out: list[dict] = []
    root = ElementTree.fromstring(raw)
    for citation in root.iter("PubmedArticle"):
        try:
            medline = citation.find("MedlineCitation")
            if medline is None:
                continue
            pmid_el = medline.find("PMID")
            article = medline.find("Article")
            if pmid_el is None or article is None:
                continue
            pmid = (pmid_el.text or "").strip()
            title = _strip_tags(article.find("ArticleTitle"))

            journal_el = article.find("Journal")
            journal = _strip_tags(journal_el.find("ISOAbbreviation")) if journal_el is not None else ""
            if not journal and journal_el is not None:
                journal = _strip_tags(journal_el.find("Title"))

            authors: list[str] = []
            author_list = article.find("AuthorList")
            if author_list is not None:
                for a in author_list.findall("Author"):
                    last = _strip_tags(a.find("LastName"))
                    initials = _strip_tags(a.find("Initials"))
                    coll = _strip_tags(a.find("CollectiveName"))
                    if last:
                        authors.append(f"{last} {initials}".strip())
                    elif coll:
                        authors.append(coll)
            author_str = ", ".join(authors[:3])
            if len(authors) > 3:
                author_str += ", et al."

            pub_date = ""
            for art_date in article.findall("ArticleDate"):
                y = _strip_tags(art_date.find("Year"))
                m = _strip_tags(art_date.find("Month"))
                d = _strip_tags(art_date.find("Day"))
                if y:
                    pub_date = "-".join([y, (m or "01").zfill(2), (d or "01").zfill(2)])
                    break
            if not pub_date and journal_el is not None:
                ji = journal_el.find("JournalIssue/PubDate")
                if ji is not None:
                    y = _strip_tags(ji.find("Year"))
                    med = _strip_tags(ji.find("MedlineDate"))
                    if y:
                        pub_date = f"{y}-01-01"
                    elif med:
                        pub_date = med

            parts: list[str] = []
            abstract_el = article.find("Abstract")
            if abstract_el is not None:
                for sec in abstract_el.findall("AbstractText"):
                    label = sec.get("Label")
                    text = _strip_tags(sec)
                    if text:
                        parts.append(f"{label.upper()}: {text}" if label else text)
            abstract = re.sub(r"\s+", " ", " ".join(parts)).strip()

            pub_types: list[str] = []
            for pt in medline.findall("PublicationTypeList/PublicationType"):
                if pt.text:
                    pub_types.append(pt.text.strip())

            if not title:
                continue
            out.append(
                {
                    "pmid": pmid,
                    "title": title,
                    "journal": journal,
                    "authors": author_str,
                    "pub_date": pub_date,
                    "abstract": abstract,
                    "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
                    "pub_types": pub_types,
                }
            )
        except Exception as exc:
            log.warning("Failed to parse one PubMed record: %s", exc)
    return out


def score_article(article: dict, cfg: dict) -> int:
    text = f"{article['title']} {article['abstract']}".lower()
    journal = (article.get("journal") or "").lower()
    sc = cfg["scoring"]
    score = 0
    for term in sc["high_value"]:
        if term in text:
            score += 5
    for term in sc["moderate_value"]:
        if term in text:
            score += 2
    if journal and any(pj in journal for pj in cfg["priority_journals"]):
        score += sc["journal_bonus"]
    if any("practice guideline" in pt.lower() for pt in article.get("pub_types", [])):
        score += sc["guideline_bonus"]
    return min(score, 60)


# ---------------------------------------------------------------------------
# LLM summaries (Mistral API)
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = (
    "You are a clinical literature assistant for a psychiatry resident "
    "(ST-läkare i psykiatri, Sweden). Summarize psychiatric research papers "
    "in ENGLISH. Be precise, balanced and factual; never overstate findings. "
    "Note the study type (e.g. RCT, meta-analysis, cohort) and mention key "
    "effect sizes, numbers or time frames when given. Always flag important "
    "limitations if evident from the abstract.\n"
    "Use exactly this format with these three lines:\n"
    "BACKGROUND: <one sentence>\n"
    "FINDINGS: <1-3 sentences>\n"
    "CLINICAL RELEVANCE: <1 sentence on what it means for clinical practice>"
)


def fallback_summary(article: dict) -> str:
    text = article.get("abstract") or ""
    if not text:
        return "(No abstract available — see the original article.)"
    snippet = text[:600]
    if len(text) > 600:
        cut = snippet.rfind(". ")
        if cut > 120:
            snippet = snippet[: cut + 1]
        else:
            snippet += " […]"
    return snippet


class MistralClient:
    def __init__(self, cfg: dict):
        self.cfg = cfg["llm"]
        self.api_key = self.cfg.get("api_key") or os.environ.get("MISTRAL_API_KEY", "")
        self.model = self.cfg.get("model", "mistral-small-latest")

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def summarize(self, article: dict) -> str:
        user_msg = (
            f"Title: {article['title']}\n"
            f"Journal: {article.get('journal', '')}\n"
            f"Abstract: {article.get('abstract', '') or '(none)'}\n\n"
            "Summarize according to the required format."
        )
        body = {
            "model": self.model,
            "temperature": 0.2,
            "max_tokens": 320,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
        }
        last_err = None
        for attempt in range(3):
            try:
                resp = requests.post(
                    "https://api.mistral.ai/v1/chat/completions",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json=body,
                    timeout=60,
                )
                if resp.status_code in (429, 500, 502, 503, 504):
                    wait = 2 ** attempt * 3
                    log.warning("Mistral API %s, retrying in %ss", resp.status_code, wait)
                    time.sleep(wait)
                    continue
                resp.raise_for_status()
                return resp.json()["choices"][0]["message"]["content"].strip()
            except Exception as exc:
                last_err = exc
                time.sleep(2 ** attempt * 3)
        raise RuntimeError(f"Mistral summarization failed: {last_err}")


# ---------------------------------------------------------------------------
# State + output files
# ---------------------------------------------------------------------------

def data_path() -> str:
    return os.path.join(DOCS_DIR, "data.json")


def load_state() -> dict:
    if os.path.exists(data_path()):
        try:
            with open(data_path(), "r", encoding="utf-8") as fh:
                return json.load(fh)
        except Exception as exc:
            log.error("Could not read data.json (%s); starting fresh", exc)
    return {"generated_at": None, "articles": []}


def save_state(state: dict) -> None:
    os.makedirs(DOCS_DIR, exist_ok=True)
    with open(data_path(), "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=1)


def write_rss(state: dict, cfg: dict) -> None:
    items = [a for a in state["articles"] if a.get("summary")][: cfg["site"]["rss_limit"]]
    xml_items = []
    for a in items:
        desc = (a.get("summary") or "").replace("<", "&lt;").replace(">", "&gt;")
        xml_items.append(
            "<item>"
            f"<title>{xml_escape(a['title'])}</title>"
            f"<link>{xml_escape(a['url'])}</link>"
            f"<guid isPermaLink='false'>psydigest-{xml_escape(a['pmid'])}</guid>"
            f"<description>{xml_escape(desc)}</description>"
            f"<category>{xml_escape(a.get('category', 'Research'))}</category>"
            "</item>"
        )
    site_title = xml_escape(cfg["site"]["title"])
    base = xml_escape(cfg["site"].get("base_url", ""))
    link_el = f"<link>{base}</link>" if base else ""
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss version="2.0"><channel>'
        f"<title>{site_title} — psychiatry literature digest</title>"
        f"{link_el}"
        "<description>Summaries of new psychiatric research and guidelines</description>"
        f"<lastBuildDate>{datetime.now(timezone.utc).strftime('%a, %d %b %Y %H:%M:%S GMT')}</lastBuildDate>"
        + "".join(xml_items)
        + "</channel></rss>"
    )
    with open(os.path.join(DOCS_DIR, "feed.xml"), "w", encoding="utf-8") as fh:
        fh.write(xml)


INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PsyDigest — psychiatry literature digest</title>
<link rel="alternate" type="application/rss+xml" title="PsyDigest RSS" href="feed.xml">
<style>
  :root { --ink:#1a2233; --muted:#6b7688; --line:#e3e8ef; --accent:#2c5cc5;
          --bg:#f6f8fb; --card:#fff; --tagbg:#eef2fb; --tagfg:#2c5cc5; }
  @media (prefers-color-scheme: dark) {
    :root { --ink:#e7ecf3; --muted:#93a0b4; --line:#2a3342; --accent:#7fa4f0;
            --bg:#12161d; --card:#1a2029; --tagbg:#232f47; --tagfg:#8fb1f5; }
  }
  * { box-sizing: border-box; }
  body { margin:0; font-family:-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
         color:var(--ink); background:var(--bg); line-height:1.55; }
  header { background:var(--card); border-bottom:1px solid var(--line); padding:14px 20px;
           position:sticky; top:0; z-index:5; }
  h1 { font-size:19px; margin:0 0 4px; letter-spacing:.3px; }
  h1 span { color:var(--accent); }
  .meta { color:var(--muted); font-size:12.5px; }
  .controls { display:flex; gap:10px; flex-wrap:wrap; align-items:center; margin-top:10px; }
  input[type=search] { flex:1; min-width:160px; padding:8px 12px; border:1px solid var(--line);
                       border-radius:8px; font-size:14px; background:var(--card); color:var(--ink); }
  .chip { border:1px solid var(--line); background:var(--card); color:var(--muted);
          border-radius:99px; padding:5px 13px; font-size:13px; cursor:pointer; }
  .chip.on { background:var(--tagbg); color:var(--tagfg); border-color:var(--tagfg); }
  main { max-width:820px; margin:0 auto; padding:20px; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:10px;
          padding:15px 17px; margin-bottom:13px; }
  .card h3 { margin:0 0 5px; font-size:16px; }
  .card h3 a { color:var(--ink); text-decoration:none; }
  .card h3 a:hover { color:var(--accent); }
  .tag { display:inline-block; font-size:11px; background:var(--tagbg); color:var(--tagfg);
         border-radius:99px; padding:2px 9px; margin-right:6px; }
  .pill { font-size:12px; color:var(--muted); }
  .summary { margin-top:7px; font-size:14.3px; }
  .summary p { margin:3px 0; }
  .summary b { color:var(--accent); }
  footer { text-align:center; color:var(--muted); font-size:12px; padding:24px; }
  .empty { text-align:center; color:var(--muted); padding:40px 0; }
  h2 { margin-top:28px; }
  ul.watch { padding-left:18px; }
  ul.watch li { margin-bottom:9px; }
  a { color:var(--accent); }
</style>
</head>
<body>
<header>
  <h1>Psy<span>Digest</span></h1>
  <div class="meta" id="stats">loading…</div>
  <div class="controls">
    <input type="search" id="q" placeholder="Search titles &amp; abstracts…">
    <button class="chip on" data-cat="all">All</button>
    <button class="chip" data-cat="Guidelines">Guidelines</button>
    <button class="chip" data-cat="Research">Research</button>
  </div>
</header>
<main>
  <div id="list"></div>
  <h2>Guideline sources to check manually</h2>
  <ul class="watch" id="watch"></ul>
</main>
<footer>PsyDigest · data from PubMed E-utilities · summaries via Mistral AI ·
  <a href="feed.xml">RSS feed</a></footer>
<script>
'use strict';
let ARTICLES = [];
let WATCH = [];
let activeCat = 'all';

function esc(s) {
  const d = document.createElement('div');
  d.textContent = s == null ? '' : String(s);
  return d.innerHTML;
}

function summaryHtml(s) {
  if (!s) return '';
  const out = [];
  for (let line of String(s).split('\\n')) {
    line = line.trim();
    if (!line) continue;
    const m = line.match(/^(BACKGROUND|FINDINGS|CLINICAL RELEVANCE)\\s*:\\s*(.+)$/i);
    if (m) out.push('<p><b>' + m[1].toUpperCase() + ':</b> ' + esc(m[2]) + '</p>');
    else out.push('<p>' + esc(line) + '</p>');
  }
  return out.join('');
}

function card(a) {
  const tag = a.category === 'Guidelines' ? 'Guidelines' : 'Research';
  const date = a.pub_date || (a.added || '').slice(0, 10);
  return '<div class="card">' +
    '<h3><a href="' + esc(a.url) + '" target="_blank" rel="noopener">' + esc(a.title) + ' ↗</a></h3>' +
    '<span class="meta">' + esc(a.authors) + (a.journal ? ' · ' + esc(a.journal) : '') +
    (date ? ' · ' + esc(date) : '') + '</span><br>' +
    '<span class="tag">' + tag + '</span> <span class="pill">relevance ' + a.score + '</span>' +
    '<div class="summary">' + summaryHtml(a.summary) + '</div>' +
    '</div>';
}

function render() {
  const q = document.getElementById('q').value.trim().toLowerCase();
  const list = ARTICLES.filter(a => {
    if (activeCat !== 'all' && a.category !== activeCat) return false;
    if (!q) return true;
    return (a.title + ' ' + (a.abstract || '') + ' ' + (a.summary || '')).toLowerCase().includes(q);
  });
  document.getElementById('list').innerHTML = list.length
    ? list.map(card).join('')
    : '<div class="empty">No articles match.</div>';
}

fetch('data.json').then(r => r.json()).then(data => {
  ARTICLES = data.articles || [];
  WATCH = data.watch_links || [];
  const n = ARTICLES.length;
  const llm = ARTICLES.filter(a => a.summarized).length;
  const gen = data.generated_at ? data.generated_at.replace('T', ' ').slice(0, 16) + ' UTC' : 'never';
  document.getElementById('stats').textContent =
    n + ' articles (last ' + (data.retention_days || 90) + ' days) · ' + llm + ' with LLM summary · updated ' + gen;
  document.getElementById('watch').innerHTML = WATCH.map(w =>
    '<li><a href="' + esc(w.url) + '" target="_blank" rel="noopener">' + esc(w.name) + '</a>' +
    (w.note ? '<br><span class="meta">' + esc(w.note) + '</span>' : '') + '</li>'
  ).join('');
  render();
});

document.getElementById('q').addEventListener('input', render);
document.querySelectorAll('.chip').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.chip').forEach(b => b.classList.remove('on'));
    btn.classList.add('on');
    activeCat = btn.dataset.cat;
    render();
  });
});
</script>
</body>
</html>
"""


def write_index() -> None:
    with open(os.path.join(DOCS_DIR, "index.html"), "w", encoding="utf-8") as fh:
        fh.write(INDEX_HTML)


# ---------------------------------------------------------------------------
# Digest cycle
# ---------------------------------------------------------------------------

def run_digest(cfg: dict) -> dict:
    state = load_state()
    known = {a["pmid"] for a in state["articles"]}
    stats = {"found": 0, "new": 0, "summarized": 0}

    client = PubMedClient(cfg)
    llm = MistralClient(cfg)

    # Search all queries; first run backfills further back
    first_run = not state["articles"]
    pmid_category: dict[str, str] = {}
    for query in cfg["queries"]:
        if first_run:
            days = cfg["pubmed"].get("initial_backfill_days", 14)
        elif query.get("category") == "Guidelines":
            days = cfg["pubmed"].get("guideline_days_back", 30)
        else:
            days = cfg["pubmed"].get("days_back", 3)
        try:
            pmids = client.search(
                query["term"], days_back=days,
                max_records=cfg["pubmed"].get("max_records_per_query", 100),
            )
            log.info("Query '%s' (%d days): %d hits", query["name"], days, len(pmids))
        except Exception as exc:
            log.error("Query '%s' failed: %s", query["name"], exc)
            continue
        category = query.get("category", "Research")
        for pmid in pmids:
            pmid_category.setdefault(pmid, category)
            if category == "Guidelines":
                pmid_category[pmid] = category
    stats["found"] = len(pmid_category)

    new_pmids = [p for p in pmid_category if p not in known]
    if new_pmids:
        try:
            fresh = client.fetch_details(new_pmids)
        except Exception as exc:
            log.error("efetch failed: %s", exc)
            fresh = []
        fresh.sort(key=lambda a: score_article(a, cfg), reverse=True)
        if not llm.enabled:
            log.warning("MISTRAL_API_KEY not set — using abstract excerpts instead.")
        budget = cfg["llm"].get("max_summaries_per_run", 25)
        today = datetime.now(timezone.utc).date().isoformat()
        for art in fresh:
            score = score_article(art, cfg)
            summary, summarized = "", 0
            if art["abstract"] and score >= cfg["llm"].get("min_score_for_summary", 6):
                if llm.enabled and budget > 0:
                    try:
                        summary = llm.summarize(art)
                        summarized = 1
                        budget -= 1
                        stats["summarized"] += 1
                    except Exception as exc:
                        log.error("Summary failed for PMID %s: %s", art["pmid"], exc)
                if not summary:
                    summary = fallback_summary(art)
            elif art["abstract"]:
                summary = fallback_summary(art)
            state["articles"].append(
                {
                    "pmid": art["pmid"],
                    "title": art["title"],
                    "journal": art["journal"],
                    "authors": art["authors"],
                    "pub_date": art["pub_date"],
                    "abstract": (art["abstract"] or "")[:1500],
                    "url": art["url"],
                    "category": pmid_category.get(art["pmid"], "Research"),
                    "score": score,
                    "summary": summary,
                    "summarized": summarized,
                    "added": today,
                }
            )
            stats["new"] += 1

    # Newest first, then apply retention window
    state["articles"].sort(key=lambda a: (a.get("added", ""), a.get("score", 0)), reverse=True)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=cfg["site"]["retention_days"])).date().isoformat()
    state["articles"] = [a for a in state["articles"] if a.get("added", "") >= cutoff]
    state["generated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    state["retention_days"] = cfg["site"]["retention_days"]
    state["watch_links"] = cfg["watch_links"]

    save_state(state)
    write_rss(state, cfg)
    write_index()
    log.info("Digest complete: %s", stats)
    return stats


def main() -> None:
    cfg = load_config()
    if "--check" in sys.argv:
        state = load_state()
        write_rss(state, cfg)
        write_index()
        log.info("Rebuilt static site from existing data.json (%d articles)", len(state["articles"]))
        return
    run_digest(cfg)


if __name__ == "__main__":
    main()
