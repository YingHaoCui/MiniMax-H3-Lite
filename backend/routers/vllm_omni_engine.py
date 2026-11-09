"""Remote vLLM-Omni video jobs: multipart submission, polling and download.

run() blocks a queue worker, never the FastAPI request thread. POST is never
retried: a lost response may still represent an accepted, expensive job.
"""
import logging
import mimetypes
import random
import time
import uuid
from contextlib import ExitStack
from pathlib import Path
from urllib.parse import quote

import httpx
from config import settings

_log = logging.getLogger(__name__)


def backoff_delay(attempt, base=1.0, cap=30.0):
    return min(max(base * 2 ** max(attempt, 0) * (0.8 + random.random() * 0.4), 0.05), cap)


def check_health():
    """Require a successful health response and the configured model identity."""
    endpoint = settings.VLLM_OMNI_ENDPOINT.rstrip("/")
    try:
        with httpx.Client(timeout=5, trust_env=False) as client:
            client.get(f"{endpoint}/health").raise_for_status()
            response = client.get(f"{endpoint}/v1/models")
            response.raise_for_status()
            models = [item.get("id") for item in response.json().get("data", [])]
            if settings.VLLM_OMNI_MODEL and settings.VLLM_OMNI_MODEL not in models:
                return {"reachable": False, "detail": f"Model mismatch: expected {settings.VLLM_OMNI_MODEL}, got {models}"}
        return {"reachable": True, "detail": f"vllm-omni ready: {models}"}
    except Exception as exc:
        return {"reachable": False, "detail": f"{type(exc).__name__}: {exc}"}


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("vllm-omni job polling deadline exceeded")
    return min(remaining, settings.VLLM_OMNI_TIMEOUT)


def _get(client, url, deadline):
    for attempt in range(settings.VLLM_OMNI_RETRIES + 1):
        try:
            response = client.get(url, timeout=_remaining(deadline))
            response.raise_for_status()
            return response
        except httpx.TransportError:
            if attempt == settings.VLLM_OMNI_RETRIES:
                raise
            time.sleep(min(backoff_delay(attempt), _remaining(deadline)))


def _form(params):
    data = {
        "prompt": params.get("prompt", ""),
        "aspect_ratio": params.get("aspect_ratio", "16:9"),
        "short_edge": params.get("short_edge", min(params.get("width", 1344), params.get("height", 768))),
    }
    for source, target in (("width", "width"), ("height", "height"),
                           ("num_frames", "num_frames"), ("fps", "fps"),
                           ("seed", "seed"), ("steps", "num_inference_steps")):
        if params.get(source) is not None:
            data[target] = params[source]
    # num_frames already contains the H3 frame-grid rounding; avoid conflicting seconds.
    if "num_frames" not in data and params.get("duration") is not None:
        data["seconds"] = params["duration"]
    return {key: str(value) for key, value in data.items()}


def run(params):
    """Return a local MP4 path for inference.py to archive."""
    paths = [params[key] for key in ("first_image", "last_image") if params.get(key)]
    if not paths:
        refs = params.get("refs") or []
        if any(ref.get("kind") != "image" for ref in refs):
            raise ValueError("This vllm-omni contract only supports image references")
        paths = [ref["path"] for ref in refs]
    if params.get("task_type") == "fl2va" and not paths:
        raise ValueError("I2V requires at least one reference image")
    endpoint = settings.VLLM_OMNI_ENDPOINT.rstrip("/")
    health = check_health()
    if not health["reachable"]:
        raise RuntimeError(f"vllm-omni 服务不可达或模型不匹配：{health['detail']}")
    deadline = time.monotonic() + settings.VLLM_OMNI_JOB_TIMEOUT
    tmp_path = settings.ASSETS_DIR / f"tmp_{uuid.uuid4().hex}.mp4"
    job_id = None
    try:
        with ExitStack() as stack:
            client = stack.enter_context(httpx.Client(timeout=settings.VLLM_OMNI_TIMEOUT, trust_env=False))
            # Encoding form fields as multipart parts also forces multipart for T2V.
            parts = [(key, (None, value)) for key, value in _form(params).items()]
            field = "input_reference" if len(paths) == 1 else "input_references"
            for value in paths:
                path = Path(value)
                stream = stack.enter_context(path.open("rb"))
                parts.append((field, (path.name, stream, mimetypes.guess_type(path.name)[0] or "application/octet-stream")))
            response = client.post(f"{endpoint}/v1/videos", files=parts, timeout=_remaining(deadline))
            response.raise_for_status()
            job = response.json()
            job_id = job.get("id")
            if not isinstance(job_id, str) or not job_id:
                raise RuntimeError("vllm-omni response missing job id")
            _log.info("vllm-omni submitted job_id=%s", job_id)
            job_url = f"{endpoint}/v1/videos/{quote(job_id, safe='')}"
            while True:
                status = job.get("status")
                if status == "completed":
                    break
                if status == "failed":
                    raise RuntimeError(f"remote job failed: {job.get('error', job)}")
                if status not in ("queued", "in_progress"):
                    raise RuntimeError(f"Unknown remote job status: {status!r}")
                time.sleep(min(settings.VLLM_OMNI_POLL_INTERVAL, _remaining(deadline)))
                job = _get(client, job_url, deadline).json()
            video = _get(client, f"{job_url}/content", deadline)
            if "video/mp4" not in video.headers.get("content-type", "") or not video.content:
                raise RuntimeError("vllm-omni returned empty or non-MP4 content")
            tmp_path.write_bytes(video.content)
        return str(tmp_path)
    except Exception as exc:
        tmp_path.unlink(missing_ok=True)
        raise RuntimeError(f"vllm-omni 推理失败 (job_id={job_id}): {exc}") from exc
