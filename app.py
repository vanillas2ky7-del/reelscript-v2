"""릴스크립트 v2: server-side Instagram extraction and high-accuracy Korean ASR."""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse

from instagram_extract import DownloadError, download_reel, normalize_instagram_url

ROOT = Path(__file__).parent
MAX_UPLOAD_BYTES = 250 * 1024 * 1024
MAX_DURATION_SEC = 15 * 60
TTL_SECONDS = 60 * 60
ALLOWED_EXTS = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".mp3", ".m4a", ".wav", ".ogg", ".aac", ".flac"}
LOG = logging.getLogger("reelscript")
logging.basicConfig(level=logging.INFO)

app = FastAPI(title="릴스크립트 v2", version="2.0.0")
EXECUTOR = ThreadPoolExecutor(max_workers=int(os.getenv("JOB_WORKERS", "2")))
JOBS: dict[str, dict] = {}
LOCK = threading.Lock()


@app.get("/")
def homepage():
    return FileResponse(ROOT / "index.html")


@app.get("/static/{filename}")
def static(filename: str):
    if filename not in {"style.css", "app.js"}:
        raise HTTPException(404)
    return FileResponse(ROOT / filename)


@app.get("/api/health")
def health():
    return {
        "ok": True,
        "transcription_configured": bool(os.environ.get("OPENAI_API_KEY")),
        "ffmpeg_installed": shutil.which("ffmpeg") is not None,
    }


def _update(job_id: str, stage: str, percent: int, **extra):
    with LOCK:
        if job_id in JOBS:
            JOBS[job_id].update({"stage": stage, "progress": percent, **extra})


def _run_checked(args: list[str], timeout: int = 150):
    try:
        return subprocess.run(args, capture_output=True, text=True, check=True, timeout=timeout)
    except FileNotFoundError as exc:
        raise RuntimeError("FFmpeg가 서버에 설치되지 않았습니다.") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("영상 처리 제한 시간을 초과했습니다.") from exc
    except subprocess.CalledProcessError as exc:
        tail = (exc.stderr or "")[-500:]
        LOG.warning("media tool failed: %s", tail)
        raise RuntimeError("영상에서 소리를 추출하지 못했습니다. 파일에 재생 가능한 음성이 있는지 확인해주세요.") from exc


def _duration(path: Path) -> float:
    result = _run_checked([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(path),
    ], 30)
    try:
        sec = float(result.stdout.strip())
    except ValueError as exc:
        raise RuntimeError("영상 길이를 확인할 수 없습니다.") from exc
    if sec <= 0 or sec > MAX_DURATION_SEC:
        raise RuntimeError("15분 이하의 영상 또는 음성 파일만 지원합니다.")
    return sec


def _convert_audio(path: Path, dest: Path, enhanced: bool = False):
    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-i", str(path), "-vn"]
    if enhanced:
        command += ["-af", "highpass=f=85,lowpass=f=7600,afftdn=nf=-25,loudnorm=I=-18:TP=-2:LRA=11"]
    command += ["-ac", "1", "-ar", "24000", "-codec:a", "libmp3lame", "-b:a", "96k", str(dest)]
    _run_checked(command)
    if not dest.exists() or dest.stat().st_size < 2000:
        raise RuntimeError("음성 데이터가 너무 짧거나 없습니다.")
    if dest.stat().st_size > 24 * 1024 * 1024:
        raise RuntimeError("압축된 음성이 API의 25MB 제한을 초과했습니다.")


def _transcribe(client: OpenAI, mp3: Path, hints: str) -> str:
    # The current gpt-transcribe API uses languages[] (not singular language).
    context = (
        "This audio is a Korean short-form video narration. Produce a faithful, verbatim Korean "
        "transcription, including the speaker's actual word choice. Preserve natural Korean "
        "spacing, particles, punctuation, and spoken quantities. Do not add anything not said."
    )
    if hints.strip():
        context += " Likely proper nouns or specialist vocabulary (hints only, not required output): " + hints.strip()[:500]
    extra_body = {"languages": ["ko"]}
    keywords = [w.strip().replace("<", "").replace(">", "")[:80] for w in re.split(r"[,\n;]+", hints) if w.strip()]
    if keywords:
        extra_body["keywords"] = keywords[:24]
    with mp3.open("rb") as audio_file:
        result = client.audio.transcriptions.create(
            model=os.getenv("TRANSCRIBE_MODEL", "gpt-transcribe"),
            file=audio_file,
            prompt=context,
            extra_body=extra_body,
            timeout=210,
        )
    return (result.text or "").strip()


def _reconcile(client: OpenAI, raw: str, cleaned: str, hints: str) -> str:
    if not cleaned or raw.strip() == cleaned.strip():
        return raw
    instructions = (
        "너는 두 개의 동일 음원 전사본을 비교하는 한국어 교열자다. A는 원본 음원, B는 약한 노이즈 제거 음원에서 나온 결과다. "
        "최종 결과는 실제 발화에 최대한 충실하게 만들되 A를 기본으로 삼고 B로 명백한 잘못 들은 단어/조사/띄어쓰기를 정정한다. "
        "원문에 없는 문장/설명/제품 기능/추측을 절대로 덧붙이지 말 것. 브랜드명 등 근거 없는 복원을 하지 말 것. "
        "의심스럽고 두 결과가 다른 경우 A를 유지한다. 자연스러운 문장부호만 보완한다. 결과에는 최종 대본만 출력한다."
    )
    user_text = f"A (원본):\n{raw}\n\nB (노이즈 감소):\n{cleaned}"
    if hints:
        user_text += f"\n\n참고 단어(반드시 쓰라는 의미 아님): {hints[:500]}"
    result = client.responses.create(
        model=os.getenv("PROOFREAD_MODEL", "gpt-4.1"),
        instructions=instructions,
        input=user_text,
        temperature=0,
        timeout=120,
    )
    return (result.output_text or raw).strip()


def _work(job_id: str, directory: str, url: str | None, uploaded_path: str | None, mode: str, hints: str):
    try:
        folder = Path(directory)
        if url:
            _update(job_id, "인스타그램 영상에 연결하는 중…", 12)
            media_path = download_reel(url, folder)
        else:
            _update(job_id, "업로드된 파일을 확인하는 중…", 12)
            media_path = Path(uploaded_path)

        _update(job_id, "영상 길이 및 소리 확인 중…", 24)
        _duration(media_path)
        raw_audio = folder / "audio_original.mp3"
        _convert_audio(media_path, raw_audio)

        from openai import OpenAI
        client = OpenAI(api_key=os.environ["OPENAI_API_KEY"], max_retries=2)
        _update(job_id, "고정밀 한국어 1차 인식 중…", 42)
        primary = _transcribe(client, raw_audio, hints)
        if not primary:
            raise RuntimeError("음성이 감지되지 않았습니다. 나레이션이 있는 영상인지 확인해주세요.")
        final = primary
        second = None
        if mode == "accurate":
            _update(job_id, "노이즈를 줄여 2차 인식 중…", 62)
            enhanced = folder / "audio_enhanced.mp3"
            _convert_audio(media_path, enhanced, enhanced=True)
            second = _transcribe(client, enhanced, hints)
            _update(job_id, "두 결과를 비교하고 오타를 교정하는 중…", 85)
            final = _reconcile(client, primary, second, hints)

        _update(job_id, "완료", 100, state="done", result={
            "transcript": final,
            "raw_transcript": primary,
            "second_transcript": second,
            "source": "link" if url else "upload",
            "canonical_url": url,
            "mode": mode,
        })
    except DownloadError as exc:
        _update(job_id, "링크 처리 실패", 100, state="failed", error=str(exc), code="instagram_restricted")
    except Exception as exc:
        LOG.exception("Job %s failed", job_id)
        message = str(exc)
        if "insufficient_quota" in message:
            message = "OpenAI API의 사용 가능 잔액을 확인해주세요. ChatGPT 구독과 API 요금은 별도입니다."
        elif "invalid_api_key" in message or "Incorrect API key" in message:
            message = "서버에 설정된 OPENAI_API_KEY가 올바르지 않습니다."
        elif "rate_limit_exceeded" in message or "Rate limit" in message:
            message = "음성 인식 요청 제한에 도달했습니다. 잠시 후 다시 시도해주세요."
        elif "OPENAI_API_KEY" in message:
            message = "서버에 OPENAI_API_KEY 환경변수가 필요합니다."
        else:
            # Don't return secrets or internal traces in generic errors.
            if not isinstance(exc, (RuntimeError, ValueError)):
                message = "처리 중 오류가 발생했습니다. 영상 형식과 API 설정을 확인해주세요."
        _update(job_id, "처리 실패", 100, state="failed", error=message, code="processing_error")
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def _cleanup_jobs():
    now = time.time()
    with LOCK:
        for job_id, job in list(JOBS.items()):
            if job["created"] < now - TTL_SECONDS and job["state"] in ("done", "failed"):
                del JOBS[job_id]


@app.post("/api/jobs", status_code=202)
async def submit(
    url: str = Form(default=""),
    video: UploadFile | None = File(default=None),
    mode: str = Form(default="accurate"),
    hints: str = Form(default=""),
):
    if not os.getenv("OPENAI_API_KEY"):
        raise HTTPException(503, "서버 관리자에게 OPENAI_API_KEY 설정이 필요합니다.")
    if mode not in ("fast", "accurate"):
        raise HTTPException(400, "올바르지 않은 인식 모드입니다.")
    if bool(url.strip()) == bool(video and video.filename):
        raise HTTPException(400, "릴스 링크 또는 영상 파일 중 하나만 입력해주세요.")
    if len(hints) > 1000:
        raise HTTPException(400, "참고 단어는 1,000자 이하로 입력해주세요.")
    canonical = None
    if url.strip():
        try:
            canonical = normalize_instagram_url(url)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
    tempdir = tempfile.mkdtemp(prefix="reelscript_")
    uploaded_path = None
    try:
        if video and video.filename:
            ext = Path(video.filename).suffix.lower()
            if ext not in ALLOWED_EXTS:
                raise HTTPException(400, "지원하지 않는 파일 형식입니다. MP4, MOV, M4A, MP3, WAV 등을 사용해주세요.")
            uploaded_path = str(Path(tempdir) / ("user_upload" + ext))
            total = 0
            with open(uploaded_path, "wb") as sink:
                while chunk := await video.read(1024 * 1024):
                    total += len(chunk)
                    if total > MAX_UPLOAD_BYTES:
                        raise HTTPException(413, "250MB 이하 파일만 업로드할 수 있습니다.")
                    sink.write(chunk)
            if total == 0:
                raise HTTPException(400, "빈 파일은 업로드할 수 없습니다.")
    except Exception:
        shutil.rmtree(tempdir, ignore_errors=True)
        raise
    finally:
        if video:
            await video.close()
    _cleanup_jobs()
    job_id = uuid.uuid4().hex
    with LOCK:
        JOBS[job_id] = {"state": "queued", "stage": "요청 접수 완료", "progress": 0, "created": time.time()}
    EXECUTOR.submit(_work, job_id, tempdir, canonical, uploaded_path, mode, hints)
    return {"job_id": job_id}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    if not re.fullmatch(r"[a-f0-9]{32}", job_id):
        raise HTTPException(404, "작업을 찾을 수 없습니다.")
    with LOCK:
        job = JOBS.get(job_id)
        if job and job["state"] in ("done", "failed") and job["created"] < time.time() - TTL_SECONDS:
            del JOBS[job_id]
            job = None
        if not job:
            raise HTTPException(404, "작업을 찾을 수 없습니다. 다시 실행해주세요.")
        return JSONResponse({k: v for k, v in job.items() if k != "created"}, headers={"Cache-Control": "no-store"})
