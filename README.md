# ALUMNX Sales Inbox Router

**candidate_id:** `priya.sharma@gmail.com`

> Deployed backend: `REPLACE_WITH_BACKEND_URL`  
> Deployed frontend: `REPLACE_WITH_FRONTEND_URL`

---

## What It Does

Processes 150–250 daily sales emails, classifies them into 6 categories (enterprise_rfp, smb_enquiry, marketing, alliances, finance, triage), skips noise (out-of-office, newsletters, vendor spam), assigns them to the right team member, and exposes a chat interface for the Ops lead to ask analytics questions grounded in real data.

## Architecture

```
┌─────────────┐      POST /ingest       ┌──────────────────────┐
│  Frontend    │ ───────────────────────→│  FastAPI Backend     │
│  (index.html)│                         │  ┌──────────────────┐│
│              │      GET /api/tasks     │  │ Gemini 1.5 Flash ││
│              │ ←───────────────────────│  │ (LLM classifier) ││
│              │                         │  └──────────────────┘│
│              │      POST /api/chat     │  ┌──────────────────┐│
│              │ ───────────────────────→│  │ Rules Engine     ││
│              │                         │  │ (deterministic)  ││
└─────────────┘                         │  └──────────────────┘│
                                        │  ┌──────────────────┐│
                                        │  │ SQLite / Postgres ││
                                        │  │ (persistent)     ││
                                        │  └──────────────────┘│
                                        └──────────────────────┘
```

## Project Structure

```
alumnx-sales-inbox-router/
├── backend/
│   ├── app.py               # FastAPI server (Task API + ingestion + chat)
│   └── requirements.txt
├── frontend/
│   ├── index.html            # Single-page app
│   └── netlify.toml
├── scripts/
│   ├── make_eval_dataset.py  # Generate 50 labeled test emails
│   └── run_eval.py           # Run all 3 grading runs + chat tests
├── evals/
│   ├── dataset.json          # Generated test data
│   └── results.json          # Evaluation results
├── DECISIONS.md              # 6 tradeoffs with reasoning
├── EVALS.md                  # Evaluation methodology and results
├── README.md                 # This file
├── render.yaml               # Render deployment config
├── .env.example              # Environment variable template
└── .gitignore
```

## Local Setup

```bash
# 1. Clone and enter directory
git clone <repo-url>
cd alumnx-sales-inbox-router

# 2. Create virtual environment and install
python3 -m venv venv
source venv/bin/activate
pip install -r backend/requirements.txt

# 3. Configure environment
cp .env.example .env
# Edit .env and add your GEMINI_API_KEY

# 4. Start the server
uvicorn app:app --app-dir backend --host 0.0.0.0 --port 8000 --reload

# 5. Open browser
open http://localhost:8000
```

## Running Evaluations

```bash
source venv/bin/activate
python3 scripts/make_eval_dataset.py    # Generate test data
python3 scripts/run_eval.py             # Run all 3 grading runs
cat evals/results.json                  # View results
```

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `GEMINI_API_KEY` | Yes* | — | Google AI API key. *System works in fallback mode without it. |
| `DATABASE_URL` | No | `sqlite:///...` | Postgres connection string for production |
| `CANDIDATE_ID` | No | `priya.sharma@gmail.com` | Default candidate identifier |
| `GEMINI_MODEL` | No | `gemini-1.5-flash` | Gemini model to use |
| `PGSSLMODE` | No | `require` | SSL mode for Postgres |

## Deployment

**Backend (Render):** Use `render.yaml` — configure `GEMINI_API_KEY` and `DATABASE_URL` as environment variables.

**Frontend (Netlify):** Deploy the `frontend/` folder. Set the API base URL in the UI to point to your Render backend.

## Task API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/tasks` | Create a task |
| `GET` | `/tasks` | List tasks (requires `candidate_id` query param) |
| `PATCH` | `/tasks/{task_id}` | Update a task |
| `DELETE` | `/tasks/{task_id}` | Delete a task |
| `GET` | `/users` | List team members |
| `POST` | `/ingest` | Ingest email batch |
| `GET` | `/api/tasks` | Get tasks + skipped for frontend |
| `GET` | `/api/stats` | Get routing statistics |
| `POST` | `/api/chat` | Chat with the routing data |
| `GET` | `/health` | Health check |
