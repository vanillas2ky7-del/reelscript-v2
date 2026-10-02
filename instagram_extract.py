"""Restrict to Instagram reels/posts and try two public extraction strategies."""
from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.parse import urlsplit, urljoin

import requests


class DownloadError(Exception):
    pass


_IG_HOSTS = {"instagram.com", "www.instagram.com", "m.instagram.com"}
_PATH = re.compile(r"^/(reel|reels|p|tv)/([a-zA-Z0-9_-]{5,30})/?$")


def normalize_instagram_url(value: str) -> str:
    """Drop tracking params (including stkn/igsh) and canonicalize the shortcode URL."""
    raw = value.strip().strip("<>\"' ")
    if not raw:
        raise ValueError("릴스 주소를 입력해주세요.")
    if raw.startswith("instagram.com/") or raw.startswith("www.instagram.com/") or raw.startswith("m.instagram.com/"):
        raw = "https://" + raw
    try:
        parsed = urlsplit(raw)
    except ValueError as exc:
        raise ValueError("인스타그램 주소 형식이 올바르지 않습니다.") from exc
    try:
        host_port = parsed.port
    except ValueError as exc:
        raise ValueError("인스타그램 주소 형식이 올바르지 않습니다.") from exc
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in _IG_HOSTS or parsed.username or parsed.password or host_port:
        raise ValueError("instagram.com의 릴스 또는 게시물 주소만 지원합니다.")
    match = _PATH.fullmatch(parsed.path)
    if not match:
        raise ValueError("릴스 주소가 아닙니다. instagram.com/reel/영상코드/ 형식으로 붙여넣어주세요.")
    kind, code = match.groups()
    kind = "reel" if kind == "reels" else kind
    return f"https://www.instagram.com/{kind}/{code}/"


def _yt_dlp_download(url: str, dest: Path) -> Path:
    import yt_dlp
    output_pattern = str(dest / "reel.%(ext)s")
    opts = {
        "outtmpl": output_pattern,
        "format": "bv*+ba/best",
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 22,
        "retries": 2,
        "fragment_retries": 2,
        "extractor_retries": 2,
        "max_filesize": 250 * 1024 * 1024,
        "http_headers": {
            "Referer": "https://www.instagram.com/",
            "User-Agent": os.getenv("INSTAGRAM_USER_AGENT", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"),
        },
    }
    cookiefile = os.getenv("INSTAGRAM_COOKIES_FILE", "").strip()
    if cookiefile and Path(cookiefile).is_file():
        opts["cookiefile"] = cookiefile
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])
    files = [p for p in dest.glob("reel.*") if p.suffix.lower() in {".mp4", ".mkv", ".webm", ".mov"}]
    if not files:
        raise DownloadError("다운로드된 영상 파일이 없습니다.")
    result = max(files, key=lambda f: f.stat().st_size)
    if result.stat().st_size > 250 * 1024 * 1024:
        raise DownloadError("영상이 250MB 제한을 넘었습니다.")
    return result


def _trusted_media_url(url: str) -> bool:
    p = urlsplit(url)
    host = p.hostname or ""
    return p.scheme == "https" and (host.endswith(".cdninstagram.com") or host == "cdninstagram.com" or host.endswith(".fbcdn.net") or host == "fbcdn.net")


def _instaloader_download(shortcode: str, dest: Path) -> Path:
    # Public post fallback; restricted posts may require a session authorized by owner.
    import instaloader
    loader = instaloader.Instaloader(quiet=True, download_pictures=False, download_video_thumbnails=False, save_metadata=False)
    username = os.getenv("IG_USERNAME", "").strip()
    session_file = os.getenv("IG_SESSION_FILE", "").strip()
    if username and session_file and Path(session_file).is_file():
        loader.load_session_from_file(username, session_file)
    post = instaloader.Post.from_shortcode(loader.context, shortcode)
    if not post.is_video:
        raise DownloadError("해당 게시물에 동영상이 없습니다.")
    video_url = post.video_url
    if not _trusted_media_url(video_url):
        raise DownloadError("인스타그램의 미디어 주소를 확인하지 못했습니다.")
    result = dest / "reel_fallback.mp4"
    current = video_url
    with requests.Session() as session:
        for hop in range(5):
            if not _trusted_media_url(current):
                raise DownloadError("인스타그램 외부로 연결되는 미디어는 처리하지 않습니다.")
            with session.get(current, stream=True, timeout=(15, 45), headers={"Referer": "https://www.instagram.com/"}, allow_redirects=False) as response:
                if response.status_code in (301, 302, 303, 307, 308):
                    redirect_to = response.headers.get("Location", "")
                    if not redirect_to:
                        raise DownloadError("인스타그램 미디어 연결이 올바르지 않습니다.")
                    current = urljoin(current, redirect_to)
                    continue
                response.raise_for_status()
                total = 0
                with result.open("wb") as target:
                    for chunk in response.iter_content(1024 * 1024):
                        if chunk:
                            total += len(chunk)
                            if total > 250 * 1024 * 1024:
                                raise DownloadError("영상이 250MB 제한을 넘었습니다.")
                            target.write(chunk)
                break
        else:
            raise DownloadError("인스타그램 미디어의 리디렉션이 너무 많습니다.")
    if result.stat().st_size == 0:
        raise DownloadError("인스타그램이 빈 영상 데이터를 반환했습니다.")
    return result


def download_reel(canonical_url: str, dest: Path) -> Path:
    """Canonical URL must already pass normalize_instagram_url()."""
    canonical_url = normalize_instagram_url(canonical_url)
    match = _PATH.fullmatch(urlsplit(canonical_url).path)
    shortcode = match.group(2)
    attempts = [canonical_url]
    # Some public Instagram media resolves via /p/ rather than /reel/.
    if "/reel/" in canonical_url:
        attempts.append(canonical_url.replace("/reel/", "/p/"))
    last_errors = []
    for attempt in attempts:
        try:
            return _yt_dlp_download(attempt, dest)
        except Exception as exc:
            last_errors.append(type(exc).__name__)
            for old in dest.glob("reel.*"):
                old.unlink(missing_ok=True)
    try:
        return _instaloader_download(shortcode, dest)
    except Exception as exc:
        last_errors.append(type(exc).__name__)
    raise DownloadError(
        "이 링크의 영상에 접근하지 못했습니다. 주소의 추적 파라미터 문제는 아니며, "
        "비공개/로그인 요구/지역 또는 요청 제한이 있을 수 있습니다. "
        "공개 릴스인데 실패했다면 서버의 Instagram 세션 설정을 확인하거나, 영상을 기기에 저장해 파일 업로드를 이용해주세요."
    )
