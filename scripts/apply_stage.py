"""
Stages a Greenhouse job application in a remote browser (Browserbase) so a
human can review and click Submit themselves. This script never clicks
Submit — that step is always manual.

Usage:
    python scripts/apply_stage.py --url "https://boards.greenhouse.io/company/jobs/12345"

Requires:
    pip install playwright requests

Environment variables required:
    BROWSERBASE_API_KEY
    BROWSERBASE_PROJECT_ID
    ANTHROPIC_API_KEY       - used to draft answers to open-ended questions
"""

import argparse
import json
import os
import sys
import requests
from playwright.sync_api import sync_playwright

PROFILE_FILE = "data/applicant_profile.json"
RESUME_TEXT_FILE = "data/resume_text.txt"


def load_profile():
    with open(PROFILE_FILE, "r") as f:
        return json.load(f)


def load_resume_text():
    if os.path.exists(RESUME_TEXT_FILE):
        with open(RESUME_TEXT_FILE, "r") as f:
            return f.read()
    return ""


def create_browserbase_session():
    api_key = os.environ["BROWSERBASE_API_KEY"]
    project_id = os.environ["BROWSERBASE_PROJECT_ID"]

    resp = requests.post(
        "https://api.browserbase.com/v1/sessions",
        headers={"X-BB-API-Key": api_key},
        json={"projectId": project_id},
        timeout=30,
    )
    resp.raise_for_status()
    session = resp.json()
    connect_url = session["connectUrl"]
    live_view_url = f"https://www.browserbase.com/sessions/{session['id']}"
    return connect_url, live_view_url


def draft_answer(question_text, resume_text, job_context=""):
    """Calls Claude to draft a grounded answer to an open-ended screening
    question. Falls back to an empty string (leaving the field blank for
    manual completion) if the API call fails, rather than guessing."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return ""

    prompt = (
        "You are drafting a job application answer for the candidate below. "
        "Answer in first person, concise (3-5 sentences), grounded only in "
        "the resume content given. Do not invent employers, dates, or "
        "credentials not in the resume.\n\n"
        f"Resume:\n{resume_text}\n\n"
        f"Job context: {job_context}\n\n"
        f"Question: {question_text}\n\n"
        "Answer:"
    )

    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-sonnet-4-6",
                "max_tokens": 400,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        text_blocks = [b["text"] for b in data.get("content", []) if b.get("type") == "text"]
        return "\n".join(text_blocks).strip()
    except requests.RequestException:
        return ""


def fill_greenhouse_form(page, profile, resume_text, job_context):
    """
    Greenhouse's standard embedded application form uses consistent
    field IDs/names, which is why this targets Greenhouse specifically
    rather than trying to be universal across every ATS.
    """

    standard_fields = {
        "first_name": profile.get("first_name", ""),
        "last_name": profile.get("last_name", ""),
        "email": profile.get("email", ""),
        "phone": profile.get("phone", ""),
    }

    for field_name, value in standard_fields.items():
        if not value:
            continue
        try:
            locator = page.locator(f"#{field_name}, input[name='{field_name}']").first
            if locator.count() > 0:
                locator.fill(value)
        except Exception:
            pass

    try:
        resume_input = page.locator("input[type=file]").first
        if resume_input.count() > 0 and os.path.exists(profile["resume_path"]):
            resume_input.set_input_files(profile["resume_path"])
    except Exception:
        pass

    for field_name in ["linkedin", "location"]:
        value = profile.get(f"{field_name}_url" if field_name == "linkedin" else field_name, "")
        if not value:
            continue
        try:
            locator = page.get_by_label(field_name, exact=False)
            if locator.count() > 0:
                locator.first.fill(value)
        except Exception:
            pass

    # Custom screening questions: Greenhouse renders these as labeled
    # textareas under a "questions" section. Treat each labeled textarea
    # as an open question and draft an answer for it.
    try:
        textareas = page.locator("textarea")
        count = textareas.count()
        for i in range(count):
            box = textareas.nth(i)
            label_text = ""
            try:
                label_id = box.get_attribute("id")
                if label_id:
                    label_el = page.locator(f"label[for='{label_id}']")
                    if label_el.count() > 0:
                        label_text = label_el.first.inner_text()
            except Exception:
                pass

            if not label_text:
                continue  # skip unlabeled fields rather than guess blindly

            answer = draft_answer(label_text, resume_text, job_context)
            if answer:
                box.fill(answer)
    except Exception:
        pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True, help="Greenhouse job application page URL")
    args = parser.parse_args()

    profile = load_profile()
    resume_text = load_resume_text()
    connect_url, live_view_url = create_browserbase_session()

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(connect_url)
        context = browser.contexts[0]
        page = context.pages[0] if context.pages else context.new_page()

        page.goto(args.url, wait_until="domcontentloaded", timeout=30000)

        job_title = ""
        try:
            job_title = page.locator("h1").first.inner_text()
        except Exception:
            pass

        fill_greenhouse_form(page, profile, resume_text, job_context=job_title)

        print("Application staged for review (not submitted). Open this link to review and submit:")
        print(live_view_url)

    webhook = os.environ.get("SLACK_WEBHOOK_URL")
    if webhook:
        requests.post(
            webhook,
            json={"text": f"Application staged — review and submit yourself: {live_view_url}\nJob: {args.url}"},
            timeout=10,
        )


if __name__ == "__main__":
    sys.exit(main())
