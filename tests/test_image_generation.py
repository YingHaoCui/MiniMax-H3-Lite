"""Image request contracts, persistence, validation, and uncertain POST recovery."""
import base64
import io
import json
from contextlib import closing

import httpx
import pytest
from PIL import Image

from config import settings, Settings
from database import get_db
from routers import queue_manager as qm
from routers.inference import run_inference


def png(color=(20, 80, 120, 90)):
    buffer = io.BytesIO()
    Image.new('RGBA', (32, 32), color).save(buffer, format='PNG')
    return buffer.getvalue()


@pytest.fixture
def image_shot(client, new_project, monkeypatch):
    monkeypatch.setattr(qm, 'enqueue', lambda _: None)
    return client.post(f'/api/projects/{new_project}/shots', json={'mode': 'image'}).json()['id']


def submit(client, sid, **kwargs):
    return client.post('/api/image-generations', json={'shot_id': sid, 'prompt': '画一只猫', 'seed': 0, **kwargs})


def transport(monkeypatch, reply):
    requests = []
    real = httpx.Client

    def handle(request):
        requests.append(request)
        if isinstance(reply, Exception):
            raise reply
        return reply

    def create(**kwargs):
        assert kwargs['trust_env'] is False
        return real(transport=httpx.MockTransport(handle), **kwargs)
    monkeypatch.setattr(httpx, 'Client', create)
    return requests


def test_environment_configuration(monkeypatch):
    for key, value in {'IMAGE_MODEL': 'custom-image', 'IMAGE_ENDPOINT': 'http://image',
                       'IMAGE_TIMEOUT': '120', 'IMAGE_STEPS': '28', 'IMAGE_CFG_SCALE': '1.2',
                       'IMAGE_API_KEY': 'secret'}.items():
        monkeypatch.setenv('MMH3_' + key, value)
    config = Settings.from_env()
    assert (config.IMAGE_MODEL, config.IMAGE_ENDPOINT, config.IMAGE_TIMEOUT, config.IMAGE_STEPS) == ('custom-image', 'http://image', 120, 28)
    assert config.IMAGE_CFG_SCALE == 1.2


def test_t2i_archive_and_restart(client, image_shot, monkeypatch):
    monkeypatch.setattr(settings, 'IMAGE_MODEL', 'configured-model')
    monkeypatch.setattr(settings, 'IMAGE_ENDPOINT', '')
    monkeypatch.setattr(settings, 'VLLM_OMNI_ENDPOINT', 'http://shared-origin')
    monkeypatch.setattr(settings, 'IMAGE_API_KEY', 'secret')
    task = submit(client, image_shot).json()
    assert task['mode'] == 't2i'
    assert submit(client, image_shot).json()['id'] == task['id']
    assert 'secret' not in client.get('/api/images/config').text
    # Configuration is snapshotted when queued, not re-resolved when a worker runs.
    monkeypatch.setattr(settings, 'IMAGE_MODEL', 'changed')
    content = png()
    requests = transport(monkeypatch, httpx.Response(200, json={'data': [{'b64_json': base64.b64encode(content).decode()}]}))
    result = run_inference(task['id'])
    assert run_inference(task['id']) == result
    assert len(requests) == 1
    request = requests[0]
    assert str(request.url) == 'http://shared-origin/v1/images/generations'
    body = json.loads(request.content)
    assert body['model'] == 'configured-model' and body['seed'] == 0
    assert body['size'] == '1024x1024' and body['num_inference_steps'] == 40
    assert request.headers['authorization'] == 'Bearer secret'
    assert (settings.BASE_DIR / result['path']).read_bytes() == content
    with closing(get_db()) as db:
        asset = db.execute('SELECT * FROM assets WHERE id=?', (result['asset_id'],)).fetchone()
        assert asset['mime'] == 'image/png'
        assert json.loads(asset['meta'])['width'] == 32
    assert client.get('/api/generations/' + task['id']).json()['result_path'] == result['path']


def test_ti2i_original_images_in_order(client, image_shot, monkeypatch):
    originals = [png(), png((100, 120, 50, 0))]
    ids = [client.post('/api/upload', data={'shot_id': image_shot}, files={'file': (f'{i}.png', data, 'image/png')}).json()['id'] for i, data in enumerate(originals)]
    task = submit(client, image_shot, ref_ids=ids[::-1]).json()
    assert task['mode'] == 'ti2i'
    requests = transport(monkeypatch, httpx.Response(200, json={'data': [{'b64_json': base64.b64encode(png()).decode()}]}))
    run_inference(task['id'])
    request = requests[0]
    assert request.url.path == '/v1/images/edits'
    assert request.headers['content-type'].startswith('multipart/form-data;')
    assert request.content.count(b'name="image"') == 2
    assert request.content.index(originals[1]) < request.content.index(originals[0])
    assert b'name="seed"\r\n\r\n0\r\n' in request.content
    assert b'name="num_inference_steps"\r\n\r\n40\r\n' in request.content


@pytest.mark.parametrize('overrides', [
    {'prompt': ' '}, {'width': 1025}, {'width': 0}, {'height': 4096}, {'steps': 0},
    {'seed': -1}, {'ref_ids': ['a'] * 5}, {'ref_ids': ['a', 'a']}, {'ref_ids': ['missing']},
])
def test_invalid_requests(client, image_shot, overrides):
    assert submit(client, image_shot, **overrides).status_code == 422
    with closing(get_db()) as db:
        assert db.execute('SELECT COUNT(*) FROM generation_tasks').fetchone()[0] == 0


def test_fake_image_rejected(client, image_shot):
    asset = client.post('/api/upload', data={'shot_id': image_shot}, files={'file': ('bad.png', b'not png', 'image/png')}).json()
    assert submit(client, image_shot, ref_ids=[asset['id']]).status_code == 422


@pytest.mark.parametrize('reply', [
    httpx.ReadTimeout('lost response'), httpx.Response(500),
    httpx.Response(200, json={'data': [{'b64_json': 'not-base64'}]}),
    httpx.Response(200, json={'data': [{'b64_json': base64.b64encode(b'not png').decode()}]}),
])
def test_failed_or_uncertain_response_never_reposts(client, image_shot, monkeypatch, reply):
    task = submit(client, image_shot).json()
    requests = transport(monkeypatch, reply)
    with pytest.raises(Exception):
        run_inference(task['id'])
    with pytest.raises(RuntimeError, match='阻止自动重发'):
        run_inference(task['id'])
    assert len(requests) == 1
    with closing(get_db()) as db:
        assert db.execute("SELECT COUNT(*) FROM assets WHERE kind='result'").fetchone()[0] == 0


def test_image_worker_never_uses_local_engine_timeout(client, image_shot, monkeypatch):
    task = submit(client, image_shot).json()
    calls = []

    def run(tid, *, remote=False):
        calls.append(remote)
        raise RuntimeError('remote failed')
    monkeypatch.setattr(qm, '_run_with_timeout', run)
    monkeypatch.setattr(settings, 'CHECKPOINT_DIR', settings.BASE_DIR / 'checkpoints')
    monkeypatch.setattr(qm, '_checkpoint', None)
    monkeypatch.setattr(settings, 'TASK_RETRY_MAX', 3)
    qm._run_task(task['id'])
    assert calls == [True]
    assert client.get('/api/generations/' + task['id']).json()['status'] == 'failed'


def test_page_and_config(client):
    page = client.get('/images')
    assert page.status_code == 200
    assert 'T2I / TI2I' in page.text
    assert client.get('/api/images/config').json()['model'] == settings.IMAGE_MODEL
