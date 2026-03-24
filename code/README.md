# 🛡️ GigShield — Vercel Deployment Guide

## Deploy in 3 steps

### Step 1 — Push to GitHub
```bash
git init
git add .
git commit -m "GigShield Phase 2"
git remote add origin https://github.com/YOUR_USERNAME/gigshield.git
git push -u origin main
```

### Step 2 — Import to Vercel
1. Go to https://vercel.com/new
2. Click **"Import Git Repository"**
3. Select your `gigshield` repo
4. Vercel auto-detects the config from `vercel.json`
5. Click **Deploy** — done in ~60 seconds

### Step 3 — (Optional) Add Live Weather
In Vercel dashboard → Project → Settings → Environment Variables:
```
OWM_API_KEY = your_openweathermap_key
```
Free key at: https://openweathermap.org/api  
Without it → realistic per-city mock data is used automatically ✅

---

## Project Structure

```
gigshield-vercel/
├── vercel.json          ← Vercel routing config
├── requirements.txt     ← Python deps (FastAPI + Mangum)
├── api/
│   └── index.py         ← Entire backend (FastAPI + Mangum handler)
└── public/
    └── index.html       ← Full frontend SPA
```

## How it works on Vercel

| Part | How |
|------|-----|
| Frontend (`public/`) | Vercel static CDN |
| Backend (`api/index.py`) | Python serverless function |
| Routing | `vercel.json` → `/api/*` → Python, `/*` → static |
| Database | In-memory per function instance (demo) |

## Important: Data Persistence

Vercel functions are **stateless** — data resets on cold starts. This is fine for:
- Hackathon demos ✅
- Judge walkthroughs ✅

For production persistence, add a free database:
- **Neon** (Postgres, free tier): https://neon.tech
- **Supabase** (Postgres, free tier): https://supabase.com
- **PlanetScale** (MySQL, free tier): https://planetscale.com

Then set `DATABASE_URL` env var and swap the in-memory `DB` dict in `api/index.py` with SQLAlchemy calls.

## Local Development

```bash
# Install Vercel CLI
npm i -g vercel

# Run locally (mirrors Vercel exactly)
vercel dev

# Or run backend directly
pip install -r requirements.txt
uvicorn api.index:app --reload --port 8000
# Open public/index.html in browser
```

## API Endpoints (all at `/api/...`)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/health` | Health check |
| GET | `/api/weather/{city}` | Live weather + active triggers |
| POST | `/api/register` | Register worker + create policy |
| GET | `/api/policy/{id}` | Fetch policy details |
| POST | `/api/calculate-premium` | AI dynamic premium |
| POST | `/api/start-shift` | Start GPS shift |
| POST | `/api/end-shift` | End shift + fraud score |
| POST | `/api/trigger-event` | Fire parametric trigger → auto-claim |
| GET | `/api/claims/{id}` | All claims |
| GET | `/api/claims/{id}/week` | This week's claims |
| POST | `/api/weekly-payout` | Run Friday payout batch |
| GET | `/api/payouts/{id}` | Payout history |

---

*GigShield — Protecting India's delivery backbone, one week at a time.*
