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
            "fps": 24, "num_frames": 107, "seed": 0, "steps": 20, "image_preprocess": "original", **overrides}


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


def _persistent_task():
    from database import get_db
    with get_db() as db:
        db.execute("INSERT INTO projects(id) VALUES ('p')")
        db.execute("INSERT INTO shots(id,project_id) VALUES ('s','p')")
        db.execute("INSERT INTO generation_tasks(id,shot_id,status) VALUES ('t','s','processing')")


def test_restart_resumes_remote_id_and_endpoint(remote, temp_db_path, monkeypatch):
    import remote_jobs
    requests, responses, _ = remote
    _persistent_task()
    responses.extend([
        httpx.Response(202, json={"id": "video_saved", "status": "queued"}),
        httpx.Response(200, json={"status": "in_progress", "progress": 42}),
        httpx.ReadError("connection lost"),
    ])
    with pytest.raises(RuntimeError, match="connection lost"):
        ve.run(params(task_type="t2va", task_id="t"))
    saved = remote_jobs.load("t")
    assert saved["remote_job_id"] == "video_saved"
    assert saved["progress"] == 42
    assert saved["remote_endpoint"] == "http://remote"
    monkeypatch.setattr(ve.settings, "VLLM_OMNI_ENDPOINT", "http://replacement")
    responses.extend([httpx.Response(200, json={"status": "completed"}), video()])
    ve.run(params(task_type="fl2va", task_id="t"))  # no original files needed
    assert len([r for r in requests if r.method == "POST"]) == 1
    assert requests[-1].url.host == "remote"
    assert requests[-1].url.path == "/v1/videos/video_saved/content"


def test_uncertain_submission_is_not_replayed(remote, temp_db_path):
    import remote_jobs
    requests, responses, _ = remote
    _persistent_task()
    responses.append(httpx.ReadTimeout("response lost"))
    with pytest.raises(RuntimeError):
        ve.run(params(task_type="t2va", task_id="t"))
    assert remote_jobs.load("t")["remote_phase"] == "submitting"
    with pytest.raises(RuntimeError, match="outcome unknown"):
        ve.run(params(task_type="t2va", task_id="t"))
    assert len([r for r in requests if r.method == "POST"]) == 1


@pytest.mark.parametrize('mode',['resize','crop'])
def test_processed_double_images_sent_as_png(remote, mode):
    from email import policy
    from email.parser import BytesParser
    from io import BytesIO
    from PIL import Image
    requests,responses,directory=remote
    paths=[directory/'wide.jpg',directory/'tall.png']
    for path,size in zip(paths,[(300,100),(100,300)]):
        Image.new('RGB',size,'green').save(path)
    originals=[path.read_bytes() for path in paths]
    responses.extend([httpx.Response(200,json={'id':'x','status':'completed'}),video()])
    ve.run(params(first_image=str(paths[0]),last_image=str(paths[1]),width=64,height=96,image_preprocess=mode))
    request=next(r for r in requests if r.method=='POST')
    message=BytesParser(policy=policy.default).parsebytes(
        ('Content-Type: '+request.headers['content-type']+'\r\nMIME-Version: 1.0\r\n\r\n').encode()+request.content)
    files=[part for part in message.iter_parts() if part.get_filename()]
    assert len(files)==2
    for part in files:
        assert part.get_param('name',header='content-disposition')=='input_references'
        assert part.get_content_type()=='image/png'
        with Image.open(BytesIO(part.get_payload(decode=True))) as im:
            assert im.size==(64,96)
    assert [path.read_bytes() for path in paths]==originals
    assert b'name="image_preprocess"' not in request.content  # client-only option


def test_invalid_processed_image_never_submitted(remote):
    requests,responses,directory=remote
    path=directory/'invalid.png';path.write_bytes(b'not an image')
    with pytest.raises(RuntimeError):
        ve.run(params(first_image=str(path),image_preprocess='crop'))
    assert not any(r.method=='POST' for r in requests)


def test_omitted_mode_defaults_to_center_crop(remote):
    from PIL import Image
    from email import policy
    from email.parser import BytesParser
    from io import BytesIO
    requests,responses,directory=remote
    path=directory/'source.png'
    with Image.new('RGB',(300,100),'red') as source:
        source.paste('green',(100,0,200,100));source.save(path)
    responses.extend([httpx.Response(200,json={'id':'x','status':'completed'}),video()])
    payload=params(first_image=str(path),width=100,height=100)
    payload.pop('image_preprocess')
    ve.run(payload)
    request=next(r for r in requests if r.method=='POST')
    message=BytesParser(policy=policy.default).parsebytes(
        ('Content-Type: '+request.headers['content-type']+'\r\n\r\n').encode()+request.content)
    part=next(p for p in message.iter_parts() if p.get_filename())
    with Image.open(BytesIO(part.get_payload(decode=True))) as result:
        assert result.size==(100,100)
        assert result.getpixel((0,0))==(0,128,0)
        assert result.getpixel((99,99))==(0,128,0)
