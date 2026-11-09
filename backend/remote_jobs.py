"""Durable remote submission journal. Never replay an uncertain POST."""
from contextlib import closing
from database import get_db


def load(task_id):
    with closing(get_db()) as db:
        row = db.execute("SELECT * FROM generation_tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise RuntimeError(f"Task no longer exists: {task_id}")
        return dict(row)


def begin(task_id, endpoint, model):
    with closing(get_db()) as db, db:
        changed = db.execute(
            "UPDATE generation_tasks SET remote_endpoint=?, remote_model=?, remote_phase='submitting' "
            "WHERE id=? AND remote_phase IS NULL", (endpoint, model, task_id),
        ).rowcount
        if changed != 1:
            raise RuntimeError("Remote submission already started; refusing duplicate POST")


def accepted(task_id, job_id):
    with closing(get_db()) as db, db:
        if db.execute(
            "UPDATE generation_tasks SET remote_job_id=?, remote_phase='polling' WHERE id=?",
            (job_id, task_id),
        ).rowcount != 1:
            raise RuntimeError(f"Cannot persist remote job {job_id}: task deleted")


def progress(task_id, job):
    value = max(0, min(99, int(job.get("progress") or 0)))
    phase = {"completed": "downloading", "failed": "failed"}.get(job.get("status"), "polling")
    with closing(get_db()) as db, db:
        db.execute(
            "UPDATE generation_tasks SET progress=MAX(progress,?), remote_phase=? "
            "WHERE id=? AND status IN ('pending','processing')", (value, phase, task_id),
        )
