"""Cloud workbench regressions: concurrency, restart and persisted media."""
import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from threading import Barrier

from database import get_db
from routers import queue_manager as qm
from services.generation_service import GenRequest, submit_generation


def seed_shot():
    with closing(get_db()) as db, db:
        db.execute("INSERT INTO projects(id) VALUES ('p')")
        db.execute("INSERT INTO shots(id,project_id) VALUES ('s','p')")


def test_simultaneous_submit_is_atomic(temp_db_path, monkeypatch):
    seed_shot()
    enqueued = []
    monkeypatch.setattr(qm, "enqueue", enqueued.append)
    barrier = Barrier(2)
    def submit():
        barrier.wait(timeout=5)
        return submit_generation("test", GenRequest(shot_id="s", prompt="hello"))["id"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(submit) for _ in range(2)]
        ids = [future.result(timeout=10) for future in futures]
    assert ids[0] == ids[1]
    assert enqueued == [ids[0]]


def test_shot_media_and_history_survive_reload(client, temp_db_path):
    seed_shot()
    with closing(get_db()) as db, db:
        db.execute("INSERT INTO assets(id,kind,path,mime,meta) VALUES ('image','image','uploads/frame.png','image/png','{}')")
        db.execute("INSERT INTO shot_refs(shot_id,asset_id,ref_type) VALUES ('s','image','image')")
        db.execute("INSERT INTO assets(id,kind,path,mime,meta) VALUES ('result','result','assets/video.mp4','video/mp4',?)",
                   (json.dumps({"thumbnail": "assets/thumb.jpg"}),))
        db.execute("INSERT INTO generation_tasks(id,shot_id,status,result_asset_id) VALUES ('t','s','completed','result')")
        db.execute("UPDATE shots SET result_asset_id='result',status='completed' WHERE id='s'")
    for _ in range(2):
        shot = client.get('/api/projects/p/shots').json()[0]
        assert shot['refs'][0]['path'] == 'uploads/frame.png'
        assert shot['result_path'] == 'assets/video.mp4'
        assert shot['latest_task']['id'] == 't'
        history = client.get('/api/projects/p/history').json()[0]
        assert history['meta']['thumbnail'] == 'assets/thumb.jpg'


def test_invalid_i2v_reference_rejected(client, temp_db_path, monkeypatch):
    seed_shot()
    monkeypatch.setattr(qm, 'enqueue', lambda _: None)
    reply = client.post('/api/generations', json={
        'shot_id': 's', 'mode': 'first_frame', 'ref_ids': ['missing'],
    })
    assert reply.status_code == 422
    with closing(get_db()) as db:
        assert db.execute('SELECT COUNT(*) FROM generation_tasks').fetchone()[0] == 0


def test_recovered_remote_task_skips_local_gpu_timeout(temp_db_path, monkeypatch):
    seed_shot()
    with closing(get_db()) as db, db:
        db.execute("INSERT INTO generation_tasks(id,shot_id,remote_endpoint) VALUES ('t','s','http://original')")
    monkeypatch.setattr(qm, 'run_inference', lambda _: {'asset_id': 'a'})
    assert qm._run_with_timeout('t', remote=True) == {'asset_id': 'a'}


def test_enqueue_deduplicates_until_worker_finishes(monkeypatch):
    monkeypatch.setattr(qm, '_worker_started', True)
    monkeypatch.setattr(qm, '_queue', [])
    monkeypatch.setattr(qm, '_scheduled', set())
    monkeypatch.setattr(qm, '_run_task', lambda _: None)
    qm.enqueue('t')
    qm.enqueue('t')
    assert qm.queue_depth() == 1
    qm._run_scheduled('t')
    assert 't' not in qm._scheduled


def test_remote_workers_can_overlap(temp_db_path, monkeypatch):
    seed_shot()
    with closing(get_db()) as db, db:
        db.execute("INSERT INTO shots(id,project_id) VALUES ('s2','p')")
        for tid, sid in [('t1','s'), ('t2','s2')]:
            db.execute("INSERT INTO generation_tasks(id,shot_id,remote_endpoint) VALUES (?,?, 'http://remote')", (tid, sid))
    barrier = Barrier(2)
    def inference(task_id):
        barrier.wait(timeout=5)  # fails if tasks are serialized
        return {'asset_id': task_id, 'path': 'assets/test.mp4'}
    monkeypatch.setattr(qm, 'run_inference', inference)
    monkeypatch.setattr(qm, '_on_task_completed', lambda: None)
    class Checkpoint:
        def save(self, *args): pass
        def remove(self, *args): pass
    monkeypatch.setattr(qm, '_get_checkpoint', lambda: Checkpoint())
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(qm._run_task, tid) for tid in ('t1','t2')]
        for future in futures:
            future.result(timeout=10)
    with closing(get_db()) as db:
        assert [r[0] for r in db.execute('SELECT status FROM generation_tasks')] == ['completed','completed']


def test_dispatcher_keeps_waiting_tasks_visible(monkeypatch):
    import concurrent.futures
    import settings_store
    import pytest
    monkeypatch.setattr(qm, '_queue', ['a','b','c'])
    monkeypatch.setattr(settings_store, 'resolve', lambda *args: 2)
    submitted = []
    class Pool:
        def __init__(self, max_workers):
            assert max_workers == 2
        def submit(self, fn, task_id):
            submitted.append(task_id)
            return concurrent.futures.Future()  # deliberately unfinished
    class StopLoop(Exception): pass
    def wait(*args, **kwargs):
        assert submitted == ['a','b']
        assert qm.queue_depth() == 1
        raise StopLoop()
    monkeypatch.setattr(concurrent.futures, 'ThreadPoolExecutor', Pool)
    monkeypatch.setattr(concurrent.futures, 'wait', wait)
    with pytest.raises(StopLoop):
        qm._worker_loop()


def test_recovery_reuses_already_archived_asset(temp_db_path, monkeypatch):
    from routers import inference
    from config import settings
    seed_shot()
    media = settings.ASSETS_DIR / 'saved.mp4'
    media.write_bytes(b'video')
    with closing(get_db()) as db, db:
        db.execute("INSERT INTO assets(id,kind,path,mime) VALUES ('a','result','assets/saved.mp4','video/mp4')")
        db.execute("INSERT INTO generation_tasks(id,shot_id,status,result_asset_id) VALUES ('t','s','processing','a')")
    monkeypatch.setattr(inference, '_build_params', lambda _: (_ for _ in ()).throw(AssertionError('must reuse asset')))
    assert inference.run_inference('t') == {'asset_id':'a','path':'assets/saved.mp4'}


def test_settings_atomic_write_survives_failure(tmp_path, monkeypatch):
    import settings_store
    import pytest
    monkeypatch.setattr(settings_store, '_STORE_PATH', tmp_path / 'settings.json')
    settings_store.update({'steps': 30})
    def fail(*args): raise OSError('disk failure')
    monkeypatch.setattr(settings_store.os, 'replace', fail)
    with pytest.raises(OSError):
        settings_store.update({'steps': 50})
    assert settings_store.get('steps') == 30
    assert not list(tmp_path.glob('*.tmp'))


def test_static_bundle_and_media_can_be_separate(client, tmp_path, monkeypatch):
    from backend.main import asset_files
    bundle = tmp_path / 'web-assets'
    media = tmp_path / 'mounted-pvc'
    (bundle / 'js').mkdir(parents=True)
    media.mkdir()
    (bundle / 'js' / 'shared.js').write_text('window.ready=true;', encoding='utf-8')
    (media / 'result.mp4').write_bytes(b'video')
    monkeypatch.setattr(asset_files, 'all_directories', [str(bundle), str(media)])
    assert client.get('/assets/js/shared.js').text == 'window.ready=true;'
    assert client.get('/assets/result.mp4').content == b'video'


def test_restart_recovers_download_failure_but_not_remote_failure(temp_db_path, monkeypatch):
    seed_shot()
    with closing(get_db()) as db, db:
        for tid, phase in [('download','downloading'), ('rejected','failed')]:
            db.execute("INSERT INTO generation_tasks(id,shot_id,status,remote_job_id,remote_phase) VALUES (?, 's','failed',?,?)", (tid, tid, phase))
    class Checkpoint:
        def list(self): return []
    enqueued = []
    monkeypatch.setattr(qm, '_get_checkpoint', lambda: Checkpoint())
    monkeypatch.setattr(qm, 'enqueue', enqueued.append)
    assert qm.resume_unfinished_tasks() == ['download']
    assert enqueued == ['download']


def test_media_probe_os_failure_does_not_crash_upload(monkeypatch, tmp_path):
    from services import upload_service
    def fail(*args, **kwargs): raise OSError('invalid process handle')
    monkeypatch.setattr(upload_service.subprocess, 'run', fail)
    assert upload_service._probe_duration(tmp_path / 'video.mp4') is None


def test_v1_database_migrates_without_losing_tasks(temp_db_path):
    from database import init_db, get_schema_version, SCHEMA_VERSION
    seed_shot()
    with closing(get_db()) as db, db:
        db.execute("INSERT INTO generation_tasks(id,shot_id,payload) VALUES ('old','s','{}')")
        for column in ('remote_job_id','remote_endpoint','remote_model','remote_phase'):
            db.execute('ALTER TABLE generation_tasks DROP COLUMN ' + column)
        db.execute('PRAGMA user_version=1')
    init_db()
    with closing(get_db()) as db:
        row = dict(db.execute("SELECT * FROM generation_tasks WHERE id='old'").fetchone())
        assert row['shot_id'] == 's'
        assert row['remote_job_id'] is None
        assert row['remote_phase'] is None
        assert get_schema_version(db) == SCHEMA_VERSION


def test_follow_first_preserves_resolution_preset(temp_db_path):
    from config import settings
    from routers.inference import _build_params
    from PIL import Image
    seed_shot()
    source = settings.UPLOADS_DIR / 'first.png'
    Image.new('RGB', (800,800)).save(source)
    with closing(get_db()) as db, db:
        db.execute("INSERT INTO assets(id,kind,path,mime) VALUES ('image','image','uploads/first.png','image/png')")
        db.execute("INSERT INTO shot_refs(shot_id,asset_id,ref_type) VALUES ('s','image','image')")
    result = _build_params({'id':'t','shot_id':'s','mode':'first_frame','payload':{
        'ref_ids':['image'], 'params':{'resolution':'0.8','size_mode':'follow_first'}
    }})
    assert result['width'] == result['height'] == result['short_edge'] == 672
