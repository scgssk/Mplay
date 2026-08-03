"""
Mplay Backend – FastAPI service for YouTube audio streaming & recommendations.

Endpoints:
  GET  /api/search?query=...&limit=10      – YouTube search with fuzzy matching
  GET  /api/stream/{video_id}?quality=low  – Proxied audio stream (supports Range)
  POST /api/recommendations                 – Last.fm-based recommendations
  GET  /api/trending                        – Trending music tracks
"""

import asyncio
import hashlib
import logging
import os
import re
from typing import Optional

import httpx
from cachetools import TTLCache
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from rapidfuzz import fuzz
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

load_dotenv()

LASTFM_API_KEY = os.getenv("LASTFM_API_KEY", "")
CACHE_TTL = int(os.getenv("CACHE_TTL", "3600"))
MAX_SEARCH_RESULTS = int(os.getenv("MAX_SEARCH_RESULTS", "20"))
LASTFM_BASE = "https://ws.audioscrobbler.com/2.0/"
COOKIES_FILE = os.getenv("YOUTUBE_COOKIES_FILE")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("mplay")

# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="Mplay API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# In-memory caches
# ---------------------------------------------------------------------------

# (video_id, quality) -> {"url": str, "content_type": str, "filesize": int|None}
stream_cache: TTLCache = TTLCache(maxsize=500, ttl=CACHE_TTL)

# (query, limit) -> list[dict]
search_cache: TTLCache = TTLCache(maxsize=200, ttl=300)  # 5 min

# (artist, track) -> video_id
youtube_id_cache: TTLCache = TTLCache(maxsize=500, ttl=CACHE_TTL)

# ---------------------------------------------------------------------------
# Helpers – yt-dlp (run in thread to avoid blocking event loop)
# ---------------------------------------------------------------------------

def _ytdlp_search(query: str, limit: int) -> list[dict]:
    """Search YouTube via yt-dlp with extract_flat (no download)."""
    import yt_dlp

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "skip_download": True,
        "default_search": f"ytsearch{limit}",
        "ignoreerrors": True,
        "cookiefile": COOKIES_FILE,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        result = ydl.extract_info(f"ytsearch{limit}:{query}", download=False)

    entries = result.get("entries", []) if result else []
    tracks = []
    seen_ids = set()

    for entry in entries:
        if not entry:
            continue
        vid = entry.get("id") or entry.get("url", "")
        if not vid or vid in seen_ids:
            continue
        # Skip playlists and channels
        if entry.get("_type") in ("playlist", "channel"):
            continue
        seen_ids.add(vid)

        duration = entry.get("duration") or 0
        title = entry.get("title", "Unknown")
        uploader = entry.get("uploader") or entry.get("channel") or "Unknown"

        # Clean up artist name – remove " - Topic" suffix
        artist = re.sub(r"\s*-\s*Topic$", "", uploader, flags=re.IGNORECASE)

        view_count = entry.get("view_count")
        views_str = _format_views(view_count) if view_count else ""

        tracks.append({
            "video_id": vid,
            "title": title,
            "artist": artist,
            "duration": int(duration) if duration else 0,
            "thumbnail": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
            "views": views_str,
        })

    return tracks
  
def _ytdlp_extract_audio(video_id: str, quality: str) -> dict:
    import yt_dlp

    ydl_opts = {
        "quiet": True,
        "skip_download": True,
        "cookiefile": COOKIES_FILE,
        "extractor_args": {
            "youtube": {
                "player_client": ["android"]
            }
        }
    }

    url = f"https://www.youtube.com/watch?v={video_id}"

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)

    formats = info.get("formats", [])

    audio_formats = [
        f for f in formats
        if (
            f.get("acodec") not in (None, "none")
            and f.get("url")
        )
    ]

    if not audio_formats:
        raise ValueError("No audio formats available")

    if quality == "low":
        target = 64
    elif quality == "medium":
        target = 128
    else:
        target = 10000

    best = min(
        audio_formats,
        key=lambda f: abs((f.get("abr") or f.get("tbr") or 128) - target)
    )

    return {
        "url": best["url"],
        "content_type": best.get("mime_type", "audio/webm").split(";")[0],
        "filesize": best.get("filesize") or best.get("filesize_approx"),
    }
  
def _format_views(count: int | None) -> str:
    """Format view count to human-readable string."""
    if not count:
        return ""
    if count >= 1_000_000_000:
        return f"{count / 1_000_000_000:.1f}B"
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M"
    if count >= 1_000:
        return f"{count / 1_000:.1f}K"
    return str(count)

# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/")
async def root():
    return {"service": "Mplay API", "version": "1.0.0", "status": "running"}


@app.get("/api/search")
async def search(
    query: str = Query(..., min_length=1, description="Search term"),
    limit: int = Query(10, ge=1, le=MAX_SEARCH_RESULTS, description="Max results"),
):
    """Search YouTube for music tracks with fuzzy matching."""
    cache_key = (query.lower().strip(), limit)
    if cache_key in search_cache:
        logger.info(f"Search cache hit: {cache_key}")
        return {"results": search_cache[cache_key]}

    try:
        # Run yt-dlp in a thread to avoid blocking
        raw_results = await asyncio.to_thread(_ytdlp_search, query, limit + 10)
    except Exception as e:
        logger.error(f"YouTube search error: {e}")
        raise HTTPException(status_code=502, detail=f"YouTube search failed: {str(e)}")

    # Fuzzy filter & sort
    scored = []
    for track in raw_results:
        search_text = f"{track['title']} {track['artist']}".lower()
        score = fuzz.partial_ratio(query.lower(), search_text)
        if score >= 50:  # Lenient threshold for search
            scored.append((score, track))

    scored.sort(key=lambda x: x[0], reverse=True)
    results = [t for _, t in scored[:limit]]

    # If fuzzy matching yielded too few, include unmatched results
    if len(results) < limit:
        existing_ids = {r["video_id"] for r in results}
        for track in raw_results:
            if track["video_id"] not in existing_ids:
                results.append(track)
                if len(results) >= limit:
                    break

    search_cache[cache_key] = results
    return {"results": results}


@app.get("/api/stream/{video_id}")
async def stream(
    video_id: str,
    request: Request,
    quality: str = Query("high", regex="^(low|medium|high)$"),
):
    """Stream audio for a given YouTube video ID. Supports Range requests."""
    cache_key = (video_id, quality)

    # Check cache
    if cache_key not in stream_cache:
        try:
            info = await asyncio.to_thread(_ytdlp_extract_audio, video_id, quality)
            stream_cache[cache_key] = info
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except Exception as e:
            logger.error(f"Stream extraction error: {e}")
            raise HTTPException(status_code=502, detail=f"Could not extract audio: {str(e)}")

    cached = stream_cache[cache_key]
    audio_url = cached["url"]
    content_type = cached["content_type"]
    filesize = cached.get("filesize")

    # Build headers for proxying
    proxy_headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": "https://www.youtube.com",
        "Referer": f"https://www.youtube.com/watch?v={video_id}",
    }

    # Forward Range header if present
    range_header = request.headers.get("range")
    if range_header:
        proxy_headers["Range"] = range_header

    try:
        client = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0), follow_redirects=True)
        upstream = await client.send(
            client.build_request("GET", audio_url, headers=proxy_headers),
            stream=True,
        )
    except httpx.HTTPError as e:
        logger.error(f"Upstream fetch error: {e}")
        # Invalidate cache – URL may have expired
        stream_cache.pop(cache_key, None)
        raise HTTPException(status_code=502, detail="Failed to fetch audio stream")

    # Build response headers
    response_headers = {
        "Accept-Ranges": "bytes",
        "Content-Type": content_type,
        "Access-Control-Allow-Origin": "*",
        "Cache-Control": "no-cache",
    }

    # Forward content headers from upstream
    if "content-length" in upstream.headers:
        response_headers["Content-Length"] = upstream.headers["content-length"]
    if "content-range" in upstream.headers:
        response_headers["Content-Range"] = upstream.headers["content-range"]

    status_code = upstream.status_code  # 200 or 206

    async def generate():
        try:
            async for chunk in upstream.aiter_bytes(chunk_size=64 * 1024):
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    return StreamingResponse(
        generate(),
        status_code=status_code,
        headers=response_headers,
        media_type=content_type,
    )


# ---------------------------------------------------------------------------
# Recommendations
# ---------------------------------------------------------------------------

class RecommendationRequest(BaseModel):
    top_artists: list[str]
    limit: int = 20


@app.post("/api/recommendations")
async def recommendations(body: RecommendationRequest):
    """Generate music recommendations based on top artists via Last.fm."""
    if not body.top_artists:
        raise HTTPException(status_code=400, detail="top_artists must not be empty")

    if not LASTFM_API_KEY:
        # Fallback: search YouTube for similar music
        logger.warning("No Last.fm API key – falling back to YouTube search")
        return await _fallback_recommendations(body.top_artists, body.limit)

    try:
        similar_artists = await _get_similar_artists(body.top_artists)
        tracks = await _get_top_tracks(similar_artists)

        # Shuffle and limit
        import random
        random.shuffle(tracks)
        tracks = tracks[: body.limit]

        # Resolve YouTube video IDs
        resolved = await _resolve_youtube_ids(tracks)
        return {"recommendations": resolved}

    except Exception as e:
        logger.error(f"Recommendation error: {e}")
        # Fallback
        return await _fallback_recommendations(body.top_artists, body.limit)


async def _get_similar_artists(top_artists: list[str]) -> list[str]:
    """Get similar artists from Last.fm for each top artist."""
    similar = set()
    original_lower = {a.lower() for a in top_artists}

    async with httpx.AsyncClient(timeout=10.0) as client:
        for artist in top_artists[:3]:
            try:
                resp = await client.get(LASTFM_BASE, params={
                    "method": "artist.getSimilar",
                    "artist": artist,
                    "api_key": LASTFM_API_KEY,
                    "format": "json",
                    "limit": 3,
                })
                data = resp.json()
                artists_list = (
                    data.get("similarartists", {}).get("artist", [])
                )
                for a in artists_list:
                    name = a.get("name", "")
                    if name.lower() not in original_lower:
                        similar.add(name)
            except Exception as e:
                logger.warning(f"Last.fm getSimilar failed for '{artist}': {e}")

    return list(similar)[:9]


async def _get_top_tracks(artists: list[str]) -> list[dict]:
    """Get top tracks for each artist from Last.fm."""
    tracks = []
    async with httpx.AsyncClient(timeout=10.0) as client:
        for artist in artists:
            try:
                resp = await client.get(LASTFM_BASE, params={
                    "method": "artist.getTopTracks",
                    "artist": artist,
                    "api_key": LASTFM_API_KEY,
                    "format": "json",
                    "limit": 2,
                })
                data = resp.json()
                track_list = data.get("toptracks", {}).get("track", [])
                for t in track_list:
                    tracks.append({
                        "artist": artist,
                        "title": t.get("name", "Unknown"),
                        "duration": int(t.get("duration", 0)),
                    })
            except Exception as e:
                logger.warning(f"Last.fm getTopTracks failed for '{artist}': {e}")

    return tracks


async def _resolve_youtube_ids(tracks: list[dict]) -> list[dict]:
    """Resolve each (artist, track) pair to a YouTube video ID."""
    resolved = []

    for track in tracks:
        artist = track["artist"]
        title = track["title"]
        cache_key = (artist.lower(), title.lower())

        if cache_key in youtube_id_cache:
            vid = youtube_id_cache[cache_key]
        else:
            query = f"{artist} {title} official audio"
            try:
                results = await asyncio.to_thread(_ytdlp_search, query, 1)
                if results:
                    vid = results[0]["video_id"]
                    youtube_id_cache[cache_key] = vid
                else:
                    continue
            except Exception as e:
                logger.warning(f"YouTube resolve failed for '{artist} - {title}': {e}")
                continue

        resolved.append({
            "video_id": vid,
            "title": title,
            "artist": artist,
            "duration": track.get("duration", 0),
            "thumbnail": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
        })

    return resolved


async def _fallback_recommendations(artists: list[str], limit: int) -> dict:
    """Fallback: search YouTube for similar music when Last.fm is unavailable."""
    all_tracks = []
    for artist in artists[:3]:
        query = f"songs like {artist} official audio"
        try:
            results = await asyncio.to_thread(_ytdlp_search, query, limit // 3 + 1)
            all_tracks.extend(results)
        except Exception:
            pass

    # Deduplicate
    seen = set()
    unique = []
    for t in all_tracks:
        if t["video_id"] not in seen:
            seen.add(t["video_id"])
            unique.append(t)

    import random
    random.shuffle(unique)
    return {"recommendations": unique[:limit]}


# ---------------------------------------------------------------------------
# Trending
# ---------------------------------------------------------------------------

@app.get("/api/trending")
async def trending(limit: int = Query(20, ge=1, le=30)):
    """Return currently trending music tracks."""
    cache_key = ("__trending__", limit)
    if cache_key in search_cache:
        return {"results": search_cache[cache_key]}

    queries = [
        "trending songs tamil 2026",
        "top tamil hits 2026 official audio",
        "popular tamil music 2026",
    ]

    all_tracks = []
    seen_ids = set()

    for q in queries:
        try:
            results = await asyncio.to_thread(_ytdlp_search, q, limit)
            for t in results:
                if t["video_id"] not in seen_ids:
                    seen_ids.add(t["video_id"])
                    all_tracks.append(t)
        except Exception as e:
            logger.warning(f"Trending search failed for '{q}': {e}")

    final = all_tracks[:limit]
    search_cache[cache_key] = final
    return {"results": final}


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "caches": {
            "stream": len(stream_cache),
            "search": len(search_cache),
            "youtube_id": len(youtube_id_cache),
        },
    }
