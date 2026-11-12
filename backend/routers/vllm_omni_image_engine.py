"""Images API adapter: one POST, durable submission guard, validated PNG archive."""
import base64
import hashlib
import io
import json
from contextlib import ExitStack, closing

import httpx
from PIL import Image

from config import settings
from database import get_db
import remote_jobs


def run_task(row):
    payload = json.loads(row["payload"])
    params = payload["params"]
    tid = row["id"]
    if row.get("remote_phase"):
        raise RuntimeError("图像提交结果不明，已阻止自动重发；请检查远程服务后手动创建新任务")
    endpoint, model = payload["endpoint"], payload["model"]
    body = {"model": model, "prompt": payload["prompt"],
            "size": f'{params["width"]}x{params["height"]}', "n": 1,
            "num_inference_steps": params["steps"], "seed": params["seed"],
            "true_cfg_scale": params["true_cfg_scale"],
            "response_format": "b64_json", "output_format": "png"}
    headers = {"Authorization": f"Bearer {settings.IMAGE_API_KEY}"} if settings.IMAGE_API_KEY else {}
    with ExitStack() as stack:
        files = []
        with closing(get_db()) as db:
            for rid in payload["ref_ids"]:
                asset = db.execute("SELECT path,mime FROM assets WHERE id=?", (rid,)).fetchone()
                if asset is None:
                    raise RuntimeError("参考图片已删除")
                path = settings.BASE_DIR / asset["path"]
                files.append(("image", (path.name, stack.enter_context(path.open("rb")), asset["mime"])))
        client = stack.enter_context(httpx.Client(timeout=settings.IMAGE_TIMEOUT, trust_env=False, headers=headers))
        remote_jobs.begin(tid, endpoint, model)
        # Never retry: a timeout can still mean the server accepted an expensive request.
        if files:
            response = client.post(endpoint + "/v1/images/edits",
                                   data={k: str(v) for k, v in body.items()}, files=files)
        else:
            response = client.post(endpoint + "/v1/images/generations", json=body)
        response.raise_for_status()
    try:
        data = response.json()["data"]
        if len(data) != 1:
            raise ValueError("expected one image")
        content = base64.b64decode(data[0]["b64_json"], validate=True)
        with Image.open(io.BytesIO(content)) as im:
            im.load()
            if im.format != "PNG":
                raise ValueError("expected PNG")
            width, height = im.size
    except (KeyError, TypeError, ValueError, OSError, Image.DecompressionBombError) as exc:
        raise RuntimeError("图像服务未返回有效的 base64 PNG") from exc
    aid = "ast_" + hashlib.sha256(tid.encode()).hexdigest()[:24]
    suffix = settings.EXPLICIT_AI_LABEL_SUFFIX if settings.EXPLICIT_AI_LABEL else ""
    name = f"{aid}{suffix}.png"
    dest = settings.ASSETS_DIR / name
    temporary = dest.with_suffix(".tmp")
    try:
        temporary.write_bytes(content)
        temporary.replace(dest)
    finally:
        temporary.unlink(missing_ok=True)
    path = "assets/" + name
    meta = json.dumps({"width": width, "height": height, "seed": params["seed"],
                       "model": model, "thumbnail": path, "media_type": "image"})
    with closing(get_db()) as db, db:
        db.execute("INSERT INTO assets (id,kind,path,mime,size,meta) VALUES (?,?,?,?,?,?)",
                   (aid, "result", path, "image/png", len(content), meta))
        db.execute("UPDATE generation_tasks SET result_asset_id=?,remote_phase='completed' WHERE id=?", (aid, tid))
    return {"asset_id": aid, "path": path}
