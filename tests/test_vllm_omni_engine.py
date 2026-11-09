"""Contract tests use real httpx multipart encoding with an in-process transport."""
from pathlib import Path

import httpx
import pytest

from routers import vllm_omni_engine as ve


@pytest.fixture
def remote(monkeypatch, tmp_path):
    requests = []
    responses = []
    real_client = httpx.Client
    monkeypatch.setattr(ve.settings, "ASSETS_DIR", tmp_path)
    monkeypatch.setattr(ve.settings, "VLLM_OMNI_ENDPOINT", "http://remote")
    monkeypatch.setattr(ve.settings, "VLLM_OMNI_POLL_INTERVAL", 0)
    monkeypatch.setattr(ve.settings, "VLLM_OMNI_RETRIES", 0)

    def handle(request):
        requests.append(request)
        if request.url.path == "/health":
            return httpx.Response(200)
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": ve.settings.VLLM_OMNI_MODEL}]})
        item = responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def client(**kwargs):
        assert kwargs["trust_env"] is False
        return real_client(transport=httpx.MockTransport(handle), **kwargs)

    monkeypatch.setattr(ve.httpx, "Client", client)
    return requests, responses, tmp_path


def params(**overrides):
    return {"task_type": "fl2va", "prompt": "moving cat", "aspect_ratio": "9:16",
            "short_edge": 736, "width": 736, "height": 1280,
            "fps": 24, "num_frames": 107, "seed": 0, "steps": 20, **overrides}


def video():
    return httpx.Response(200, content=b"mp4-test-bytes", headers={"content-type": "video/mp4"})


@pytest.mark.parametrize("frames", ["first", "last", "both"])
def test_i2v_multipart_async_lifecycle(remote, frames):
    requests, responses, directory = remote
    first = directory / "first.png"
    last = directory / "last.png"
    first.write_bytes(b"FIRST_IMAGE_BYTES")
    last.write_bytes(b"LAST_IMAGE_BYTES")
    responses.extend([
        httpx.Response(202, json={"id": "video_gen_x", "status": "queued"}),
        httpx.Response(200, json={"status": "in_progress", "progress": 42}),
        httpx.Response(200, json={"status": "completed"}), video(),
    ])
    out = ve.run(params(first_image=str(first) if frames != "last" else None,
                        last_image=str(last) if frames != "first" else None))
    assert Path(out).read_bytes() == b"mp4-test-bytes"
    post = next(r for r in requests if r.method == "POST")
    assert post.headers["content-type"].startswith("multipart/form-data;")
    body = post.content
    field = b'input_references' if frames == "both" else b'input_reference'
    assert b'name="' + field + b'"' in body
    assert (b"FIRST_IMAGE_BYTES" in body) == (frames != "last")
    assert (b"LAST_IMAGE_BYTES" in body) == (frames != "first")
    assert str(directory).encode() not in body
    assert b'name="model"' not in body
    for key, value in [("aspect_ratio", "9:16"), ("short_edge", "736"), ("seed", "0"), ("num_inference_steps", "20")]:
        assert f'name="{key}"\r\n\r\n{value}\r\n'.encode() in body
    assert requests[-1].url.path == "/v1/videos/video_gen_x/content"
    assert len([r for r in requests if r.method == "POST"]) == 1


@pytest.mark.parametrize("response,match", [
    (httpx.Response(400, text="Bad request syntax"), "400"),
    (httpx.Response(200, json={"status": "queued"}), "missing job id"),
    (httpx.Response(200, json={"id": "x", "status": "failed", "error": "GPU failure"}), "GPU failure"),
    (httpx.Response(200, json={"id": "x", "status": "unexpected"}), "Unknown"),
    (httpx.ReadTimeout("lost submission response"), "lost submission"),
])
def test_failure_never_reposts(remote, response, match):
    requests, responses, directory = remote
    responses.append(response)
    with pytest.raises(RuntimeError, match=match):
        ve.run(params(task_type="t2va"))
    assert len([r for r in requests if r.method == "POST"]) == 1
    assert not list(directory.glob("tmp_*.mp4"))


@pytest.mark.parametrize("content", [
    httpx.Response(404, json={"error": "not ready"}),
    httpx.Response(200, content=b"", headers={"content-type": "video/mp4"}),
    httpx.Response(200, text="<html>bad</html>"),
])
def test_bad_download_is_not_an_asset(remote, content):
    _, responses, directory = remote
    responses.extend([httpx.Response(200, json={"id": "x", "status": "completed"}), content])
    with pytest.raises(RuntimeError):
        ve.run(params(task_type="t2va"))
    assert not list(directory.glob("tmp_*.mp4"))


def test_poll_deadline(remote, monkeypatch):
    _, responses, _ = remote
    responses.append(httpx.Response(200, json={"id": "x", "status": "queued"}))
    ticks = iter([0, 0, 1801])
    monkeypatch.setattr(ve.time, "monotonic", lambda: next(ticks))
    with pytest.raises(RuntimeError, match="deadline"):
        ve.run(params(task_type="t2va"))


def test_model_mismatch(monkeypatch):
    real_client = httpx.Client
    def handle(request):
        return httpx.Response(200, json={"data": [{"id": "wrong-model"}]})
    monkeypatch.setattr(ve.httpx, "Client", lambda **kw: real_client(transport=httpx.MockTransport(handle), **kw))
    assert ve.check_health()["reachable"] is False


def test_env_configuration(monkeypatch):
    from config import Settings
    monkeypatch.setenv("MMH3_VLLM_OMNI_ENDPOINT", "http://new-service:9098/")
    monkeypatch.setenv("MMH3_VLLM_OMNI_MODEL", "/weights/new")
    monkeypatch.setenv("MMH3_VLLM_OMNI_POLL_INTERVAL", "7")
    monkeypatch.setenv("MMH3_TASK_RETRY_MAX", "0")
    settings = Settings.from_env()
    assert settings.VLLM_OMNI_ENDPOINT == "http://new-service:9098"
    assert settings.VLLM_OMNI_MODEL == "/weights/new"
    assert settings.VLLM_OMNI_POLL_INTERVAL == 7
    assert settings.TASK_RETRY_MAX == 0
