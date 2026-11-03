import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

DB_PATH = ROOT / os.getenv("DB_PATH", "data/jobs.db")
CONFIG_PATH = ROOT / os.getenv("CONFIG_PATH", "config.yaml")

# Self-hosted LLM defaults. The link can also be set (and changed) from the web page.
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "")
LLM_MODEL = os.getenv("LLM_MODEL", "")
LLM_KIND = os.getenv("LLM_KIND", "auto")
LLM_API_KEY = os.getenv("LLM_API_KEY", "")

# Reasons offered in the "Not interested" dialog. Stored as the key; fed back to the LLM.
DECLINE_REASONS = {
    "experience_gap": "Work experience gap",
    "no_sponsorship": "No visa sponsorship",
    "seniority_too_high": "Seniority too high",
    "seniority_too_low": "Seniority too low",
    "tech_stack": "Tech stack mismatch",
    "domain": "Industry / domain not interesting",
    "location": "Location / remote policy",
    "salary": "Salary too low",
    "company": "Company not interesting",
    "other": "Other",
}


# "Posted within" choices on the Fetch dialog: key -> (label, max age in days).
# The key is also sent to MyGreenhouse as date_posted ("past_day" is confirmed working).
POSTED_WITHIN = {
    "past_day": ("Past 24 hours", 1),
    "past_week": ("Past week", 7),
    "past_month": ("Past month", 30),
    "any": ("Any time", None),
}


def load_settings() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f) or {}
