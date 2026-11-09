"""Deleting a shot must not orphan an in-flight generation."""
from contextlib import closing
import pytest
from database import get_db


def seed(status):
    with closing(get_db()) as db, db:
        db.execute("INSERT INTO projects(id) VALUES ('delete_project')")
        db.execute("INSERT INTO shots(id,project_id) VALUES ('delete_shot','delete_project')")
        db.execute("INSERT INTO assets(id,kind,path,mime) VALUES ('keep_asset','image','uploads/keep.png','image/png')")
        db.execute("INSERT INTO shot_refs(shot_id,asset_id,ref_type) VALUES ('delete_shot','keep_asset','image')")
        db.execute("INSERT INTO generation_tasks(id,shot_id,status) VALUES ('delete_task','delete_shot',?)", (status,))


@pytest.mark.parametrize('status',['pending','processing'])
def test_running_shot_cannot_be_deleted(client,status):
    seed(status)
    assert client.delete('/api/shots/delete_shot').status_code == 409
    with closing(get_db()) as db:
        assert db.execute("SELECT COUNT(*) FROM shots WHERE id='delete_shot'").fetchone()[0] == 1
        assert db.execute("SELECT status FROM generation_tasks WHERE id='delete_task'").fetchone()[0] == status


@pytest.mark.parametrize('status',['completed','failed'])
def test_delete_shot_cascades_records_preserves_media(client,status):
    seed(status)
    assert client.delete('/api/shots/delete_shot').json() == {'deleted':'delete_shot'}
    with closing(get_db()) as db:
        assert db.execute("SELECT COUNT(*) FROM generation_tasks WHERE id='delete_task'").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM shot_refs WHERE shot_id='delete_shot'").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM assets WHERE id='keep_asset'").fetchone()[0] == 1
    assert client.delete('/api/shots/delete_shot').status_code == 404
