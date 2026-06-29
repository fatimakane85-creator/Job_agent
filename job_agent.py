#!/usr/bin/env python3
"""
job_agent.py — a personal job-finding agent for QA / validation / quality roles.

What it does, end to end:
  1. Pulls fresh listings from several sources (Adzuna, JSearch, and any company
     ATS feeds you list — Greenhouse / Lever).
  2. Keeps only recent postings (you choose how many days back).
  3. Scores each job for relevance to your profile and drops the weak matches.
  4. Verifies every apply link is actually live (skips 404s AND "this job is
     closed" pages that still return 200).
  5. Writes results to a CSV and, optionally, emails you a digest.

It is built to be run on a schedule (e.g. GitHub Actions or cron) through your
networking season. Configure it with environment variables — never hard-code
keys into the file.

Dependencies:  pip install requests
"""

import os
import csv
import sys
import time
import smtplib
import datetime as dt
from email.mime.text import MIMEText
from dataclasses import dataclass, field

import requests

# ----------------------------------------------------------------------------
# CONFIG  — edit these, or override any of them with environment variables.
# ----------------------------------------------------------------------------

# Where you're looking.
COUNTRY = "ca"                 # Adzuna country code
LOCATION = "Montreal"          # city / region keyword
MAX_DAYS_OLD = 30              # only postings from the last N days

# What you're looking for. Mix English + French; these drive both the API
# queries and the relevance scoring. Keep them specific to your field.
KEYWORDS = [
    "quality assurance", "assurance qualité",
    "validation", "qualification",
    "quality specialist", "spécialiste qualité",
    "quality systems", "systèmes qualité",
    "GMP", "BPF", "GDP", "BPD",
    "process improvement", "amélioration continue",
    "regulatory compliance", "conformité réglementaire",
]

# Words in a title that strongly mean "this is for me" (boost score).
STRONG_TITLE_TERMS = [
    "quality", "qualité", "validation", "qualification",
    "qa", "quality assurance", "gmp", "bpf", "compliance", "conformité",
    "quality systems", "process improvement", "amélioration",
]

# Words that almost always mean "not for me" — drop these outright.
EXCLUDE_TERMS = [
    "senior manager", "director", "directeur", "vp ", "head of",
    "10+ years", "15+ years", "sales", "ventes",
]

# A job must reach this relevance score to be shown.
MIN_SCORE = 3

# Company ATS feeds — the highest-signal, always-current source.
# Find the "slug" in a company's careers URL. Examples:
#   Greenhouse: boards.greenhouse.io/SLUG          -> add SLUG below
#   Lever:      jobs.lever.co/SLUG                  -> add SLUG below
# These are EXAMPLES — replace with your real target employers.
GREENHOUSE_COMPANIES: list[str] = []   # e.g. ["acmebio", "examplepharma"]
LEVER_COMPANIES: list[str] = []        # e.g. ["examplepharma"]

# Output
OUTPUT_CSV = os.environ.get("OUTPUT_CSV", "jobs_found.csv")

# ---- Secrets (set as environment variables, not in this file) --------------
ADZUNA_APP_ID = os.environ.get("ADZUNA_APP_ID", "")
ADZUNA_APP_KEY = os.environ.get("ADZUNA_APP_KEY", "")
JSEARCH_API_KEY = os.environ.get("JSEARCH_API_KEY", "")   # RapidAPI key

# Email digest (optional). If these aren't set, it just writes the CSV.
EMAIL_TO = os.environ.get("EMAIL_TO", "")
EMAIL_FROM = os.environ.get("EMAIL_FROM", "")
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")   # use an app password, never your real one

HEADERS = {"User-Agent": "Mozilla/5.0 (job-agent; personal use)"}

# Phrases that mean a posting is dead even when the page returns HTTP 200.
CLOSED_PHRASES = [
    "no longer available", "no longer accepting", "position has been filled",
    "this job has expired", "job is closed", "posting is closed",
    "we are no longer", "applications are closed", "this position is closed",
    "offre n'est plus disponible", "offre expirée", "poste pourvu",
    "cette offre a été pourvue", "n'accepte plus", "candidatures sont closes",
]


@dataclass
class Job:
    title: str
    company: str
    location: str
    url: str
    source: str
    posted: str = ""
    description: str = ""
    score: int = 0
    extras: dict = field(default_factory=dict)

    def key(self) -> str:
        return f"{self.title.strip().lower()}|{self.company.strip().lower()}"


# ----------------------------------------------------------------------------
# SOURCES
# ----------------------------------------------------------------------------

def fetch_adzuna() -> list[Job]:
    if not (ADZUNA_APP_ID and ADZUNA_APP_KEY):
        return []
    jobs: list[Job] = []
    # One query per keyword keeps results focused; Adzuna ORs poorly otherwise.
    for kw in KEYWORDS:
        url = f"https://api.adzuna.com/v1/api/jobs/{COUNTRY}/search/1"
        params = {
            "app_id": ADZUNA_APP_ID,
            "app_key": ADZUNA_APP_KEY,
            "what": kw,
            "where": LOCATION,
            "max_days_old": MAX_DAYS_OLD,
            "results_per_page": 25,
            "sort_by": "date",
            "content-type": "application/json",
        }
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=20)
            r.raise_for_status()
            for it in r.json().get("results", []):
                jobs.append(Job(
                    title=it.get("title", "").replace("\n", " ").strip(),
                    company=(it.get("company") or {}).get("display_name", "—"),
                    location=(it.get("location") or {}).get("display_name", LOCATION),
                    url=it.get("redirect_url", ""),
                    source="Adzuna",
                    posted=(it.get("created", "") or "")[:10],
                    description=it.get("description", ""),
                ))
        except Exception as e:
            print(f"[Adzuna] '{kw}' failed: {e}", file=sys.stderr)
        time.sleep(0.5)  # be polite to the rate limit
    return jobs


def fetch_jsearch() -> list[Job]:
    if not JSEARCH_API_KEY:
        return []
    jobs: list[Job] = []
    date_filter = "month" if MAX_DAYS_OLD > 7 else "week"
    headers = {**HEADERS,
               "x-rapidapi-key": JSEARCH_API_KEY,
               "x-rapidapi-host": "jsearch.p.rapidapi.com"}
    # Group keywords into a few broad queries to save API calls.
    queries = ["quality assurance OR validation pharmaceutical",
               "assurance qualité OR validation pharmaceutique"]
    for q in queries:
        try:
            r = requests.get(
                "https://jsearch.p.rapidapi.com/search",
                headers=headers,
                params={"query": f"{q} {LOCATION}", "page": "1",
                        "num_pages": "1", "date_posted": date_filter,
                        "country": COUNTRY},
                timeout=25,
            )
            r.raise_for_status()
            for it in r.json().get("data", []):
                ts = it.get("job_posted_at_timestamp")
                posted = (dt.datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d")
                          if ts else "")
                jobs.append(Job(
                    title=it.get("job_title", "").strip(),
                    company=it.get("employer_name", "—"),
                    location=", ".join(filter(None, [it.get("job_city"),
                                                     it.get("job_state")])) or LOCATION,
                    url=it.get("job_apply_link", ""),
                    source=f"JSearch/{it.get('job_publisher','')}",
                    posted=posted,
                    description=it.get("job_description", "") or "",
                ))
        except Exception as e:
            print(f"[JSearch] '{q}' failed: {e}", file=sys.stderr)
        time.sleep(0.5)
    return jobs


def fetch_greenhouse(slug: str) -> list[Job]:
    jobs: list[Job] = []
    try:
        r = requests.get(
            f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
            params={"content": "true"}, headers=HEADERS, timeout=20)
        r.raise_for_status()
        for it in r.json().get("jobs", []):
            jobs.append(Job(
                title=it.get("title", "").strip(),
                company=slug,
                location=(it.get("location") or {}).get("name", ""),
                url=it.get("absolute_url", ""),
                source=f"Greenhouse/{slug}",
                posted=(it.get("updated_at", "") or "")[:10],
                description=it.get("content", "") or "",
            ))
    except Exception as e:
        print(f"[Greenhouse:{slug}] failed: {e}", file=sys.stderr)
    return jobs


def fetch_lever(slug: str) -> list[Job]:
    jobs: list[Job] = []
    try:
        r = requests.get(f"https://api.lever.co/v0/postings/{slug}",
                         params={"mode": "json"}, headers=HEADERS, timeout=20)
        r.raise_for_status()
        for it in r.json():
            cats = it.get("categories") or {}
            jobs.append(Job(
                title=it.get("text", "").strip(),
                company=slug,
                location=cats.get("location", ""),
                url=it.get("hostedUrl", ""),
                source=f"Lever/{slug}",
                posted=(dt.datetime.utcfromtimestamp(it["createdAt"] / 1000)
                        .strftime("%Y-%m-%d") if it.get("createdAt") else ""),
                description=it.get("descriptionPlain", "") or "",
            ))
    except Exception as e:
        print(f"[Lever:{slug}] failed: {e}", file=sys.stderr)
    return jobs


# ----------------------------------------------------------------------------
# FILTERING
# ----------------------------------------------------------------------------

def score(job: Job) -> int:
    hay_title = job.title.lower()
    hay_all = f"{job.title}\n{job.description}".lower()
    if any(x in hay_title for x in EXCLUDE_TERMS):
        return -1
    s = 0
    for term in STRONG_TITLE_TERMS:
        if term in hay_title:
            s += 3
    for kw in KEYWORDS:
        if kw.lower() in hay_all:
            s += 1
    return s


def is_live(url: str) -> bool:
    """True only if the link resolves AND the page isn't a closed-job page."""
    if not url:
        return False
    try:
        r = requests.get(url, headers=HEADERS, timeout=15, allow_redirects=True)
    except requests.RequestException:
        return False
    if r.status_code in (404, 410, 451):
        return False
    if r.status_code >= 500:
        return False
    body = r.text.lower()
    if any(p in body for p in CLOSED_PHRASES):
        return False
    return True


# ----------------------------------------------------------------------------
# OUTPUT
# ----------------------------------------------------------------------------

def write_csv(jobs: list[Job], path: str) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["score", "title", "company", "location", "posted", "source", "url"])
        for j in jobs:
            w.writerow([j.score, j.title, j.company, j.location, j.posted, j.source, j.url])


def email_digest(jobs: list[Job]) -> None:
    if not (EMAIL_TO and SMTP_USER and SMTP_PASS):
        return
    today = dt.date.today().isoformat()
    lines = [f"{len(jobs)} active matches — {today}", ""]
    for j in jobs:
        lines.append(f"• [{j.score}] {j.title} — {j.company} ({j.location})")
        lines.append(f"  {j.posted} · {j.source}")
        lines.append(f"  {j.url}")
        lines.append("")
    msg = MIMEText("\n".join(lines), _charset="utf-8")
    msg["Subject"] = f"Job agent: {len(jobs)} active matches ({today})"
    msg["From"] = EMAIL_FROM or SMTP_USER
    msg["To"] = EMAIL_TO
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
        s.starttls()
        s.login(SMTP_USER, SMTP_PASS)
        s.send_message(msg)
    print(f"Emailed digest to {EMAIL_TO}")


# ----------------------------------------------------------------------------
# MAIN
# ----------------------------------------------------------------------------

def main() -> None:
    raw: list[Job] = []
    raw += fetch_adzuna()
    raw += fetch_jsearch()
    for slug in GREENHOUSE_COMPANIES:
        raw += fetch_greenhouse(slug)
    for slug in LEVER_COMPANIES:
        raw += fetch_lever(slug)
    print(f"Pulled {len(raw)} raw listings.")

    # Dedupe by title+company, keeping the first seen.
    seen, deduped = set(), []
    for j in raw:
        if j.key() in seen:
            continue
        seen.add(j.key())
        deduped.append(j)

    # Score + threshold.
    relevant = []
    for j in deduped:
        j.score = score(j)
        if j.score >= MIN_SCORE:
            relevant.append(j)
    relevant.sort(key=lambda x: x.score, reverse=True)
    print(f"{len(relevant)} pass the relevance threshold. Checking links...")

    # Verify each link is live (this is the slow part — network per job).
    active = []
    for j in relevant:
        if is_live(j.url):
            active.append(j)
        else:
            print(f"  dropped (dead/closed): {j.title} — {j.company}")
        time.sleep(0.3)

    print(f"\n{len(active)} ACTIVE matches:\n")
    for j in active:
        print(f"[{j.score}] {j.title} — {j.company} ({j.location}) · {j.posted}")
        print(f"      {j.url}")

    write_csv(active, OUTPUT_CSV)
    print(f"\nSaved -> {OUTPUT_CSV}")
    email_digest(active)


if __name__ == "__main__":
    main()
