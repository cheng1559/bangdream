from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles


HOST = "127.0.0.1"
PORT = 8766
BESTDORI_ORIGIN = "https://bestdori.com"
BESTDORI_API = f"{BESTDORI_ORIGIN}/api"
BESTDORI_ASSETS = f"{BESTDORI_ORIGIN}/assets"
ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parent
CHARTS_DIR = PROJECT_ROOT / "charts"
DIFFICULTIES = {"easy", "normal", "hard", "expert", "special"}


app = FastAPI(title="BangDream Chart Viewer")


def default_server_from_song_info(info: dict) -> str:
    published_at = info.get("publishedAt") or []
    servers = ["jp", "en", "tw", "cn", "kr"]
    for index, value in enumerate(published_at):
      if value is not None and index < len(servers):
          return servers[index]
    return "jp"


def audio_asset_path(server: str, song_id: int, info: dict) -> str:
    bgm_id = info.get("bgmId") or f"bgm{song_id:03d}"
    return f"{server}/sound/{bgm_id}_rip/{bgm_id}.mp3"


async def proxy_bestdori_url(target: str, request: Request | None = None):
    headers = {
        "Accept": "*/*",
        "User-Agent": "bangdream-chart-viewer/1.0",
    }
    if request is not None:
        range_header = request.headers.get("range")
        if range_header:
            headers["Range"] = range_header

    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            upstream = await client.get(target, headers=headers)
    except httpx.RequestError as exc:
        return Response(
            content=str(exc).encode("utf-8"),
            status_code=502,
            media_type="text/plain; charset=utf-8",
        )

    content_type = upstream.headers.get("content-type", "application/json")
    response_headers = {"Cache-Control": "no-store"}
    for header in ("accept-ranges", "content-range"):
        value = upstream.headers.get(header)
        if value:
            response_headers[header] = value

    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        media_type=content_type,
        headers=response_headers,
    )


@app.get("/bestdori/api/{api_path:path}")
async def proxy_bestdori_api(api_path: str, request: Request):
    return await proxy_bestdori_url(f"{BESTDORI_API}/{api_path}", request)


@app.get("/bestdori/assets/{asset_path:path}")
async def proxy_bestdori_assets(asset_path: str, request: Request):
    return await proxy_bestdori_url(f"{BESTDORI_ASSETS}/{asset_path}", request)


@app.get("/bestdori/song-audio/{song_id}.mp3")
async def proxy_song_audio(song_id: int, request: Request):
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            info_response = await client.get(
                f"{BESTDORI_API}/songs/{song_id}.json",
                headers={"Accept": "application/json", "User-Agent": "bangdream-chart-viewer/1.0"},
            )
    except httpx.RequestError as exc:
        return Response(
            content=str(exc).encode("utf-8"),
            status_code=502,
            media_type="text/plain; charset=utf-8",
        )

    if info_response.status_code != 200:
        return Response(
            content=info_response.content,
            status_code=info_response.status_code,
            media_type=info_response.headers.get("content-type", "text/plain"),
        )

    try:
        info = info_response.json()
    except ValueError as exc:
        return Response(
            content=str(exc).encode("utf-8"),
            status_code=502,
            media_type="text/plain; charset=utf-8",
        )
    if not isinstance(info, dict):
        return Response(
            content=b"Invalid song info",
            status_code=502,
            media_type="text/plain",
        )
    server = default_server_from_song_info(info)
    target = f"{BESTDORI_ASSETS}/{audio_asset_path(server, song_id, info)}"
    return await proxy_bestdori_url(target, request)


@app.get("/charts/all.1.json")
async def get_local_song_list():
    path = CHARTS_DIR / "all.1.json"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Song list not found")
    return FileResponse(path, media_type="application/json")


@app.get("/charts/{difficulty}/{song_id}.json")
async def get_local_chart(difficulty: str, song_id: int):
    if difficulty not in DIFFICULTIES:
        raise HTTPException(status_code=400, detail="Invalid difficulty")

    path = CHARTS_DIR / difficulty / f"{song_id}.json"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Chart not found")
    return FileResponse(path, media_type="application/json")


app.mount("/", StaticFiles(directory=ROOT, html=True), name="static")


if __name__ == "__main__":
    uvicorn.run("server:app", host=HOST, port=PORT, reload=False)
