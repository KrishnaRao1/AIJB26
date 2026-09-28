"""
Job scanner: pulls open roles from a list of companies' public Greenhouse
job boards, compares against what we've already seen, and writes any new
postings to a report file. Designed to be run on a schedule by
.github/workflows/job-scan.yml

Add or remove companies in COMPANIES below. Greenhouse board tokens are
usually the slug in the company's careers URL, e.g.
https://boards.greenhouse.io/stripe -> token "stripe"
"""

import json
import os
import requests
from datetime import datetime, timezone

# --- CONFIG ------------------------------------------------------------

COMPANIES = [
    # Greenhouse board token, display name
    ("stripe", "Stripe"),
    ("airbnb", "Airbnb"),
    # add more here, e.g. ("openai", "OpenAI"),
]

# Keywords to match in job title (case-insensitive). Leave empty to keep all jobs.
TITLE_KEYWORDS = ["analyst", "data", "sports", "operations"]

STATE_FILE = "data/seen_jobs.json"
REPORT_FILE = "data/new_jobs_report.md"

# --- CORE LOGIC ----------------------------------------------------------


def fetch_jobs_for_company(token):
    url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs"
    resp = requests.get(url, timeout=15)
    resp.raise_for_status()
    return resp.json().get("jobs", [])


def title_matches(title):
    if not TITLE_KEYWORDS:
        return True
    lowered = title.lower()
    return any(kw.lower() in lowered for kw in TITLE_KEYWORDS)


def load_seen():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {}


def save_seen(seen):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(seen, f, indent=2)


def main():
    seen = load_seen()
    new_postings = []

    for token, display_name in COMPANIES:
        try:
            jobs = fetch_jobs_for_company(token)
        except requests.RequestException as e:
            print(f"[warn] failed to fetch {display_name}: {e}")
            continue

        company_seen = seen.setdefault(token, {})

        for job in jobs:
            job_id = str(job["id"])
            title = job.get("title", "")

            if job_id in company_seen:
                continue  # already logged this one

            company_seen[job_id] = {
                "title": title,
                "first_seen": datetime.now(timezone.utc).isoformat(),
            }

            if title_matches(title):
                new_postings.append(
                    {
                        "company": display_name,
                        "title": title,
                        "url": job.get("absolute_url", ""),
                    }
                )

    save_seen(seen)

    # Write a human-readable report of anything new that matched the keywords
    os.makedirs(os.path.dirname(REPORT_FILE), exist_ok=True)
    with open(REPORT_FILE, "w") as f:
        f.write(f"# New job postings\n\n")
        f.write(f"Last run: {datetime.now(timezone.utc).isoformat()}\n\n")
        if not new_postings:
            f.write("No new matching postings this run.\n")
        else:
            for job in new_postings:
                f.write(f"- **{job['company']}** — {job['title']}\n")
                f.write(f"  {job['url']}\n")

    print(f"Found {len(new_postings)} new matching postings.")

    # Optional: ping a Slack webhook if configured and there's something new
    webhook = os.environ.get("SLACK_WEBHOOK_URL")
    if webhook and new_postings:
        lines = [f"{j['company']} — {j['title']}\n{j['url']}" for j in new_postings]
        message = "New job postings found:\n\n" + "\n\n".join(lines)
        try:
            requests.post(webhook, json={"text": message}, timeout=10)
        except requests.RequestException as e:
            print(f"[warn] slack webhook failed: {e}")


if __name__ == "__main__":
    main()
