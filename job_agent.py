#!/usr/bin/env python3
"""
job_agent.py — a personal job-finding agent for QA / validation / quality roles.

Pipeline:
  1. Pull fresh listings from Adzuna, JSearch, and any company ATS feeds you add.
  2. Keep only recent postings.
  3. Score each job for relevance and drop weak / unrelated ones.
  4. Verify every apply link is actually live (skips 404s AND "this job is
     closed" pages that still return HTTP 200).
  5. Write results to: a CSV, and a self-contained HTML dashboard (docs/index.html)
     that GitHub Pages can publish. Optionally emails a digest.

Dependencies:  pip install requests
"""

import os
import csv
import sys
import time
import html
import smtplib
import datetime as dt
from email.mime.text import MIMEText
from dataclasses import dataclass, field

import requests

# ----------------------------------------------------------------------------
# CONFIG
# ----------------------------------------------------------------------------
COUNTRY = "ca"
LOCATION = "Montreal"
MAX_DAYS_OLD = 30

KEYWORDS = [
    "quality assurance", "assurance qualité",
    "validation", "qualification",
    "quality specialist", "spécialiste qualité",
    "quality systems", "systèmes qualité",
    "GMP", "BPF", "GDP", "BPD",
    "process improvement", "amélioration continue",
    "regulatory compliance", "conformité réglementaire",
]

STRONG_TITLE_TERMS = [
    "quality", "qualité", "validation", "qualification",
    "qa", "quality assurance", "gmp", "bpf", "compliance", "conformité",
    "quality systems", "process improvement", "amélioration",
]

# Cut the two big noise sources we saw: software-testing "QA" and
# non-pharma compliance (environmental / financial / legal).
EXCLUDE_TERMS = [
    "senior manager", "director", "directeur", "vp ", "head of",
    "10+ years", "15+ years", "sales", "ventes",
    "software", "logiciel", "logicielle", "firmware", "embedded", "embarqué",
    "sdet", "developer", "développeur", "dev qa", "qa automation",
    "environmental", "environnement", "sols contaminés",
    "financial", "financier", "paralegal", "marketing compliance",
]

MIN_SCORE = 3

# ---- TARGET EMPLOYERS (your shortlist) -------------------------------------
# Any posting from these firms jumps to the top of the dashboard with a star,
# and the relevance filter is loosened for them so you never miss one.
# Matching is a simple lowercase "contains" on the company name — add or
# remove names freely.
WATCHLIST = [
    # Québec — fabricants & CDMO
    "pharmascience", "jamp", "bausch", "sandoz", "delpharm", "parima",
    "duchesnay", "galderma", "theratechnologies", "liminal", "pendopharm",
    # Québec — CRO / laboratoires
    "altasciences", "charles river", "indero", "innovaderm", "royalmount",
    # Québec — conseil qualité / validation / ingénierie
    "laporte", "monbel", "novatek", "pharmalex", "propharma",
    # Distribution / services pharma
    "cencora", "innomar", "mckesson",
    # Multinationales présentes au Québec
    "pfizer", "merck", "novartis", "sanofi", "gsk", "glaxo", "haleon",
    "takeda", "roche",
    # Ontario
    "apotex", "sterimax", "novocol", "bayer",
]

GREENHOUSE_COMPANIES: list[str] = []
LEVER_COMPANIES: list[str] = []

# Output locations (docs/ is what GitHub Pages publishes)
OUTPUT_CSV = os.environ.get("OUTPUT_CSV", "docs/jobs_found.csv")
OUTPUT_HTML = os.environ.get("OUTPUT_HTML", "docs/index.html")

ADZUNA_APP_ID = os.environ.get("ADZUNA_APP_ID", "")
ADZUNA_APP_KEY = os.environ.get("ADZUNA_APP_KEY", "")
JSEARCH_API_KEY = os.environ.get("JSEARCH_API_KEY", "")

EMAIL_TO = os.environ.get("EMAIL_TO", "")
EMAIL_FROM = os.environ.get("EMAIL_FROM", "")
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")

HEADERS = {"User-Agent": "Mozilla/5.0 (job-agent; personal use)"}

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
    for kw in KEYWORDS:
        url = f"https://api.adzuna.com/v1/api/jobs/{COUNTRY}/search/1"
        params = {
            "app_id": ADZUNA_APP_ID, "app_key": ADZUNA_APP_KEY,
            "what": kw, "where": LOCATION, "max_days_old": MAX_DAYS_OLD,
            "results_per_page": 25, "sort_by": "date",
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
                    url=it.get("redirect_url", ""), source="Adzuna",
                    posted=(it.get("created", "") or "")[:10],
                    description=it.get("description", ""),
                ))
        except Exception as e:
            print(f"[Adzuna] '{kw}' failed: {e}", file=sys.stderr)
        time.sleep(0.5)
    return jobs


def fetch_jsearch() -> list[Job]:
    if not JSEARCH_API_KEY:
        return []
    jobs: list[Job] = []
    date_filter = "month" if MAX_DAYS_OLD > 7 else "week"
    headers = {**HEADERS, "x-rapidapi-key": JSEARCH_API_KEY,
               "x-rapidapi-host": "jsearch.p.rapidapi.com"}
    queries = ["quality assurance OR validation pharmaceutical",
               "assurance qualité OR validation pharmaceutique"]
    for q in queries:
        try:
            r = requests.get("https://jsearch.p.rapidapi.com/search-v2", headers=headers,
                             params={"query": f"{q} {LOCATION}", "page": "1",
                                     "num_pages": "1", "date_posted": date_filter,
                                     "country": COUNTRY}, timeout=25)
            r.raise_for_status()
            for it in r.json().get("data", []):
                ts = it.get("job_posted_at_timestamp")
                posted = (dt.datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d") if ts else "")
                jobs.append(Job(
                    title=it.get("job_title", "").strip(),
                    company=it.get("employer_name", "—"),
                    location=", ".join(filter(None, [it.get("job_city"), it.get("job_state")])) or LOCATION,
                    url=it.get("job_apply_link", ""),
                    source=f"JSearch/{it.get('job_publisher','')}",
                    posted=posted, description=it.get("job_description", "") or "",
                ))
        except Exception as e:
            print(f"[JSearch] '{q}' failed: {e}", file=sys.stderr)
        time.sleep(0.5)
    return jobs


def fetch_greenhouse(slug: str) -> list[Job]:
    jobs: list[Job] = []
    try:
        r = requests.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
                         params={"content": "true"}, headers=HEADERS, timeout=20)
        r.raise_for_status()
        for it in r.json().get("jobs", []):
            jobs.append(Job(
                title=it.get("title", "").strip(), company=slug,
                location=(it.get("location") or {}).get("name", ""),
                url=it.get("absolute_url", ""), source=f"Greenhouse/{slug}",
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
                title=it.get("text", "").strip(), company=slug,
                location=cats.get("location", ""), url=it.get("hostedUrl", ""),
                source=f"Lever/{slug}",
                posted=(dt.datetime.utcfromtimestamp(it["createdAt"] / 1000).strftime("%Y-%m-%d")
                        if it.get("createdAt") else ""),
                description=it.get("descriptionPlain", "") or "",
            ))
    except Exception as e:
        print(f"[Lever:{slug}] failed: {e}", file=sys.stderr)
    return jobs


# ----------------------------------------------------------------------------
# FILTERING
# ----------------------------------------------------------------------------
def is_target(company: str) -> bool:
    c = company.lower()
    return any(name in c for name in WATCHLIST)


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
    if not url:
        return False
    try:
        r = requests.get(url, headers=HEADERS, timeout=15, allow_redirects=True)
    except requests.RequestException:
        return False
    if r.status_code in (404, 410, 451) or r.status_code >= 500:
        return False
    body = r.text.lower()
    if any(p in body for p in CLOSED_PHRASES):
        return False
    return True


# ----------------------------------------------------------------------------
# OUTPUT
# ----------------------------------------------------------------------------
def write_csv(jobs: list[Job], path: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["score", "title", "company", "location", "posted", "source", "url"])
        for j in jobs:
            w.writerow([j.score, j.title, j.company, j.location, j.posted, j.source, j.url])


HTML_TEMPLATE = """<!doctype html>
<html lang="fr"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Mes offres — Qualité & Validation</title>
<style>
  :root{ --sage:#2F4A43; --bar:#D7E5DF; --line:#E6ECE9; --ink:#1A1A1A; --gray:#5b6b64; }
  *{box-sizing:border-box}
  body{font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;color:var(--ink);
       margin:0;background:#fbfcfb}
  .wrap{max-width:1000px;margin:0 auto;padding:28px 18px 60px}
  h1{color:var(--sage);font-size:24px;margin:0 0 4px}
  .meta{color:var(--gray);font-size:13px;margin-bottom:18px}
  .search{width:100%;padding:11px 13px;border:1px solid var(--line);border-radius:9px;
          font-size:15px;margin-bottom:14px}
  table{width:100%;border-collapse:collapse;font-size:14px;background:#fff;
        border:1px solid var(--line);border-radius:10px;overflow:hidden}
  th{background:var(--bar);color:var(--sage);text-align:left;padding:10px 12px;
     font-size:12px;letter-spacing:.04em;text-transform:uppercase;cursor:pointer;user-select:none}
  td{padding:11px 12px;border-top:1px solid var(--line);vertical-align:top}
  tr:hover td{background:#f6f9f7}
  tr.target td{background:#fbf6e9}
  tr.target:hover td{background:#f7efd8}
  .score{font-weight:700;color:var(--sage);text-align:center;width:46px}
  .src{color:var(--gray);font-size:12px;white-space:nowrap}
  a.apply{color:var(--sage);font-weight:600;text-decoration:none;white-space:nowrap}
  a.apply:hover{text-decoration:underline}
  .empty{padding:30px;text-align:center;color:var(--gray)}
  @media(max-width:640px){ .src,th.src{display:none} }
</style></head>
<body><div class="wrap">
  <h1>Mes offres — Qualité, Validation & Amélioration</h1>
  <div class="meta">{COUNT} offres actives · {TARGETS} chez mes employeurs cibles &#11088; · mise à jour {UPDATED}</div>
  <input class="search" id="q" placeholder="Filtrer (ex. validation, Pharmascience, contrat)…">
  <table id="t">
    <thead><tr>
      <th onclick="sortBy('score')">Score</th>
      <th>Poste</th><th>Entreprise</th><th>Lieu</th>
      <th onclick="sortBy('date')">Publié</th>
      <th class="src">Source</th><th>Lien</th>
    </tr></thead>
    <tbody id="b">
{ROWS}
    </tbody>
  </table>
  <div class="empty" id="none" style="display:none">Aucun résultat pour ce filtre.</div>
<script>
  const q=document.getElementById('q'), b=document.getElementById('b'), none=document.getElementById('none');
  q.addEventListener('input',()=>{const v=q.value.toLowerCase();let shown=0;
    [...b.rows].forEach(r=>{const m=r.innerText.toLowerCase().includes(v);r.style.display=m?'':'none';if(m)shown++;});
    none.style.display=shown?'none':'block';});
  let dir={};
  function sortBy(k){dir[k]=!dir[k];const rows=[...b.rows];
    rows.sort((x,y)=>{const a=k==='score'?+x.dataset.score:x.dataset.date,
      c=k==='score'?+y.dataset.score:y.dataset.date;return dir[k]?(a>c?1:-1):(a<c?1:-1);});
    rows.forEach(r=>b.appendChild(r));}
</script>
</div></body></html>
"""


def write_html(jobs: list[Job], path: str) -> None:
    rows = []
    for j in jobs:
        target = j.extras.get("target", False)
        star = "&#11088; " if target else ""        # ⭐
        rows.append(
            f'<tr class="{"target" if target else ""}" data-score="{j.score}" data-date="{html.escape(j.posted)}">'
            f'<td class="score">{j.score}</td>'
            f'<td>{star}{html.escape(j.title)}</td>'
            f'<td>{html.escape(j.company)}</td>'
            f'<td>{html.escape(j.location)}</td>'
            f'<td>{html.escape(j.posted)}</td>'
            f'<td class="src">{html.escape(j.source)}</td>'
            f'<td><a class="apply" href="{html.escape(j.url)}" target="_blank" rel="noopener">Voir &rarr;</a></td>'
            f'</tr>')
    n_target = sum(1 for j in jobs if j.extras.get("target"))
    out = (HTML_TEMPLATE
           .replace("{ROWS}", "\n".join(rows))
           .replace("{UPDATED}", dt.datetime.now().strftime("%Y-%m-%d %H:%M"))
           .replace("{COUNT}", str(len(jobs)))
           .replace("{TARGETS}", str(n_target)))
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(out)


def email_digest(jobs: list[Job]) -> None:
    if not (EMAIL_TO and SMTP_USER and SMTP_PASS):
        return
    today = dt.date.today().isoformat()
    lines = [f"{len(jobs)} active matches — {today}", ""]
    for j in jobs:
        lines += [f"• [{j.score}] {j.title} — {j.company} ({j.location})",
                  f"  {j.posted} · {j.source}", f"  {j.url}", ""]
    msg = MIMEText("\n".join(lines), _charset="utf-8")
    msg["Subject"] = f"Job agent: {len(jobs)} active matches ({today})"
    msg["From"] = EMAIL_FROM or SMTP_USER
    msg["To"] = EMAIL_TO
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
        s.starttls(); s.login(SMTP_USER, SMTP_PASS); s.send_message(msg)
    print(f"Emailed digest to {EMAIL_TO}")


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

    seen, deduped = set(), []
    for j in raw:
        if j.key() in seen:
            continue
        seen.add(j.key()); deduped.append(j)

    relevant = []
    for j in deduped:
        base = score(j)
        if base < 0:          # excluded by EXCLUDE_TERMS
            continue
        target = is_target(j.company)
        j.extras["target"] = target
        # Target firms only need a faint signal (>=1); others need MIN_SCORE.
        if base >= MIN_SCORE or (target and base >= 1):
            j.score = base + (5 if target else 0)   # boost targets to the top
            relevant.append(j)
    relevant.sort(key=lambda x: (x.extras.get("target", False), x.score), reverse=True)
    print(f"{len(relevant)} pass the relevance threshold. Checking links...")

    active = []
    for j in relevant:
        if is_live(j.url):
            active.append(j)
        else:
            print(f"  dropped (dead/closed): {j.title} — {j.company}")
        time.sleep(0.3)

    print(f"\n{len(active)} ACTIVE matches.\n")
    for j in active:
        print(f"[{j.score}] {j.title} — {j.company} ({j.location}) · {j.posted}")

    write_csv(active, OUTPUT_CSV)
    write_html(active, OUTPUT_HTML)
    print(f"Saved -> {OUTPUT_CSV} and {OUTPUT_HTML}")
    email_digest(active)


if __name__ == "__main__":
    main()
