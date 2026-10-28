# Job Search Assistant

Fetches jobs from public job APIs, drops obvious misfits with a keyword filter, has your own LLM
(for example one running on Google Colab behind a Cloudflare tunnel) score the rest against your
resume with the gaps spelled out, and shows everything on a web page where you apply or decline.
Your decisions are fed back into the next ranking. It can also write a resume tailored to any job description.

```
job APIs ──► SQLite ──► keyword filter ──► LLM scores + gaps ──► web page
 (remotive, arbeitnow,      (drop reason        (uses resume +           apply ↗ / not interested
  greenhouse, lever, ashby)  stored)             your past decisions)       (reason) ──┐
                                   ▲                                                     │
                                   └──────────────── feedback ◄──────────────────────────┘
```

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn app.main:app --reload
```

Open http://localhost:8000.

1. Click **Set link** next to "LLM" and paste your Colab Cloudflare link (`https://xxxx.trycloudflare.com`).
   **Save & test** sends a test message and shows which server type and model it found.
   The free Cloudflare link changes whenever the tunnel restarts. When it does, paste the new one here.
2. **Upload** your resume (PDF, DOCX, TXT or MD).
3. Click **Fetch & score**. New jobs are fetched, filtered, and up to `max_jobs_per_run` of them are scored.
4. Work through the **Recommended** tab: **Apply ↗** opens the posting and marks the job applied;
   **Not interested** asks for a reason (experience gap, no sponsorship, …) and an optional note.
5. **Tailor resume** writes a version of your resume for that job description (no invented facts, plus
   a "Notes for you" list of gaps it couldn't cover). You can copy it or download it as Markdown.
6. After a batch of decisions, click **Re-rank** to rescore undecided jobs using your feedback.

You can also run the pipeline from the shell (for example from cron). It uses the link saved on the
page, or `LLM_BASE_URL` from `.env` (see `.env.example`):

```bash
.venv/bin/python -m app.pipeline            # fetch + filter + score
.venv/bin/python -m app.pipeline --no-fetch # only score jobs already in the DB
```

## Which LLM servers work

The app talks plain HTTP to whatever is behind your link and detects the type automatically:

| Running in Colab | Works | Notes |
|---|---|---|
| **Ollama** (`ollama serve`) | ✓ | Tunnel port 11434. Model defaults to the first one pulled. |
| **vLLM** (`vllm serve …`) | ✓ | OpenAI-compatible. Set the API key in the dialog if you started it with `--api-key`. |
| **llama.cpp server**, LM Studio, TGI | ✓ | OpenAI-compatible (`/v1/chat/completions`). |
| A Gradio chat page | ✗ | Not supported yet. |

Model size matters. Scoring has to follow a JSON format, and small models (under about 7B) often get it
wrong. Something like Qwen2.5 7B/14B or Llama 3.1 8B works reasonably. The app tolerates sloppy JSON
(code fences, `<think>` tags, wrong labels) and retries once before marking a job as failed.

## Tabs on the page

| Tab | Shows |
|---|---|
| Recommended | Undecided jobs scoring ≥ `match_threshold` |
| Low match | Undecided jobs below the threshold, with each gap and its severity |
| Awaiting score | Passed the filter but not scored yet (over the per-run cap) |
| Applied | Jobs you clicked Apply on |
| Not interested | Declined jobs with your reason. Jobs that matched but you declined are flagged **Matched but declined** |
| Filtered out | Dropped by the keyword filter, with the rule that dropped them |
| Insights | Funnel, why matched jobs were declined, decline reasons, most common gaps, filter drops |

## Configuration

`config.yaml` sets:
- **sources**: search queries, and which company boards to pull (Greenhouse, Lever and Ashby slugs come from the careers URL).
- **filter**: title include/exclude words, description phrases that drop a job (such as "unable to sponsor"),
  location, max years of experience, max posting age.
- **llm**: jobs per run, `concurrency` (keep at 1–2 for a single Colab GPU), match threshold, and
  `max_jd_chars` (long job descriptions are cut to this length so they fit a small model's context window).

To add a job source, write a `fetch_<name>(client, cfg)` in [app/sources.py](app/sources.py) that returns
normalized job dicts, register it in `SOURCES`, and add a block under `sources:` in `config.yaml`.

## Layout

| File | Purpose |
|---|---|
| [app/sources.py](app/sources.py) | Job API adapters |
| [app/filters.py](app/filters.py) | Keyword pre-filter |
| [app/llm.py](app/llm.py) | LLM connection (Ollama / OpenAI-compatible), scoring and resume tailoring |
| [app/pipeline.py](app/pipeline.py) | fetch → filter → score |
| [app/db.py](app/db.py) | SQLite schema (`jobs`, `scores`, `resumes`, `tailored_resumes`, `settings`, `runs`) |
| [app/main.py](app/main.py) | FastAPI routes |
| [static/](static/) | Web page |

The data lives in `data/jobs.db`. Delete the file to start over.
