# PocketSmart AI

A personal smart budget planner and financial recommendation dashboard built with React, Vite, Tailwind CSS, Express, Prisma, SQLite, and Recharts.

## Local setup

Requirements: Node.js 18+ and npm.

```bash
npm install
npm run db:push
npm run db:seed
npm run dev
```

Open `http://localhost:5173` in your browser. The Vite client runs on port 5173 and the Express API runs on port 3001.

## Available scripts

- `npm run dev` - start the Vite frontend and Express API together
- `npm run build` - type-check and build the frontend
- `npm run db:push` - create/update the local SQLite database
- `npm run db:seed` - reset and seed realistic demo transactions and budgets
- `npm run db:generate` - regenerate the Prisma client

## PocketSmart AI recommendation assistant (FastAPI)

The root `app.py` also provides a server-rendered FastAPI application for home-interior, party, and jewelry recommendations. It runs alongside the existing Node dashboard and uses the same workspace without replacing the React app.

Requirements: Python 3.10+.

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
if (!(Test-Path .env)) { Copy-Item .env.example .env }
```

Add `GOOGLE_API_KEY` (or `GEMINI_API_KEY`) and a randomly generated `SECRET_KEY` of at least 32 characters to `.env`. If this workspace already has an `.env` file for the Node app, add the FastAPI keys alongside its existing values instead of replacing it. Then run:

```powershell
uvicorn app:app --reload --reload-exclude "$PWD\.venv" --port 8000
```

Open `http://127.0.0.1:8000`, create an account, and sign in. Without a Gemini key, registration, saved plans, shopping links, and budget-safe fallback recommendations still work; add a key to enable Gemini text and outfit-image recommendations. Accounts and recommendation history are stored in the ignored local `data/` folder. Uploaded outfit images are stored in `static/uploads/`. In PowerShell, the absolute virtualenv exclusion prevents dependency files from triggering reloads and expiring in-memory sessions.

The authenticated **Budget Profile** page is at `/financial-profile` (also linked in the top navigation). Enter monthly take-home income, essential expenses, and a monthly savings goal once; the app calculates `max(0, income - expenses - savings goal)` and prefills that amount in all three planners. This is a monthly estimate based on the values you provide, not a bank connection or account-balance check.

The local database is stored at `prisma/dev.db` and is ignored by git. This is intentionally a single-user local MVP; authentication is not included.
