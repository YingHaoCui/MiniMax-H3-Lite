"""Unified T2I/TI2I submission; uses the existing durable local task queue."""
import json
import secrets
from contextlib import closing

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from PIL import Image, UnidentifiedImageError

from config import settings
from database import get_db, new_id, row_to_dict

router = APIRouter()


class ImageRequest(BaseModel):
    shot_id: str
    prompt: str = Field(min_length=1, max_length=7000)
    ref_ids: list[str] = Field(default_factory=list, max_length=4)
    width: int = Field(default=1024, ge=256, le=2048)
    height: int = Field(default=1024, ge=256, le=2048)
    steps: int | None = Field(default=None, ge=1, le=100)
    seed: int | None = Field(default=None, ge=0, lt=2**32)


@router.get("/images/config")
def image_config():
    return {"model": settings.IMAGE_MODEL, "steps": settings.IMAGE_STEPS,
            "max_references": 4, "max_upload_mb": settings.MAX_UPLOAD_SIZE_MB}


@router.post("/image-generations")
def submit_image(request: Request, body: ImageRequest):
    if not body.prompt.strip() or body.width % 32 or body.height % 32:
        raise HTTPException(422, "请输入提示词；宽高须为 32 的整数倍")
    if len(set(body.ref_ids)) != len(body.ref_ids):
        raise HTTPException(422, "参考图片不能重复")
    with closing(get_db()) as db:
        shot = db.execute("SELECT * FROM shots WHERE id=?", (body.shot_id,)).fetchone()
        if not shot:
            raise HTTPException(404, "图像创作不存在")
        if shot["mode"] != "image":
            raise HTTPException(422, "请在图像生成页创建图像任务")
        for rid in body.ref_ids:
            asset = db.execute(
                "SELECT a.* FROM assets a JOIN shot_refs r ON a.id=r.asset_id "
                "WHERE r.shot_id=? AND a.id=? AND a.kind='image'", (body.shot_id, rid),
            ).fetchone()
            if not asset:
                raise HTTPException(422, "参考图片不存在或不属于当前创作")
            try:
                with Image.open(settings.BASE_DIR / asset["path"]) as im:
                    im.verify()
            except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError) as exc:
                raise HTTPException(422, "参考文件不是可读取的图片") from exc
        db.execute("BEGIN IMMEDIATE")
        active = db.execute(
            "SELECT * FROM generation_tasks WHERE shot_id=? AND status IN ('pending','processing')",
            (body.shot_id,),
        ).fetchone()
        if active:
            return row_to_dict(active)
        params = {"width": body.width, "height": body.height,
                  "steps": body.steps if body.steps is not None else settings.IMAGE_STEPS,
                  "seed": body.seed if body.seed is not None else secrets.randbits(32),
                  "true_cfg_scale": settings.IMAGE_CFG_SCALE}
        payload = {"prompt": body.prompt, "params": params, "ref_ids": body.ref_ids,
                   "endpoint": (settings.IMAGE_ENDPOINT or settings.VLLM_OMNI_ENDPOINT).rstrip("/"),
                   "model": settings.IMAGE_MODEL,
                   "request_id": getattr(request.state, "request_id", "")}
        tid = new_id("task_")
        db.execute("INSERT INTO generation_tasks (id,shot_id,mode,payload) VALUES (?,?,?,?)",
                   (tid, body.shot_id, "ti2i" if body.ref_ids else "t2i", json.dumps(payload, ensure_ascii=False)))
        db.execute("UPDATE shots SET status='queued',prompt=?,params=? WHERE id=?",
                   (body.prompt, json.dumps({**params, "ref_ids": body.ref_ids}), body.shot_id))
        db.commit()
        row = row_to_dict(db.execute("SELECT * FROM generation_tasks WHERE id=?", (tid,)).fetchone())
    from routers.queue_manager import enqueue
    try:
        enqueue(tid)
    except Exception as exc:
        with closing(get_db()) as db, db:
            db.execute("UPDATE generation_tasks SET status='failed',error=? WHERE id=?", (str(exc), tid))
            db.execute("UPDATE shots SET status='failed' WHERE id=?", (body.shot_id,))
        raise HTTPException(500, "图像任务入队失败") from exc
    return row
