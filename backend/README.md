# Mplay Backend

Lightweight FastAPI service that provides YouTube audio streaming and music recommendations.

## Setup

```bash
cd backend
pip install -r requirements.txt
```

## Environment Variables

Create a `.env` file or set in your shell / Render dashboard:

| Variable | Default | Description |
|---|---|---|
| `LASTFM_API_KEY` | _(required)_ | Last.fm API key for recommendations |
| `SECRET_KEY` | _(auto on Render)_ | Random string for future auth |
| `CACHE_TTL` | `3600` | Stream URL cache TTL in seconds |
| `MAX_SEARCH_RESULTS` | `20` | Maximum search results |

## Running Locally

```bash
# Set environment variables
set LASTFM_API_KEY=your_key_here

# Start server
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

## API Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/search?query=...&limit=10` | Search YouTube |
| `GET` | `/api/stream/{video_id}?quality=high` | Stream audio |
| `POST` | `/api/recommendations` | Get recommendations |
| `GET` | `/api/trending?limit=20` | Trending tracks |
| `GET` | `/api/health` | Health check |

## Deploy to Render

1. Push this repo to GitHub
2. On [Render](https://render.com), create a new **Web Service**
3. Connect the repo, set root directory to `backend`
4. Build: `pip install -r requirements.txt`
5. Start: `uvicorn main:app --host 0.0.0.0 --port $PORT`
6. Set `LASTFM_API_KEY` in environment variables
