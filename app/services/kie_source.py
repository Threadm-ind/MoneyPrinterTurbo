"""Generate b-roll clips with KIE.ai video models instead of stock search.

素材与脚本关键词一一对应：每个关键词提交一个文生视频任务（默认
bytedance/seedance-2-fast），轮询完成后下载为本地素材，返回与
material.download_videos 相同的本地路径列表。

费用在提交瞬间扣除，所以 taskId 必须在提交后立即落盘，且绝不重复提交。
"""

import json
import math
import os
import time

import requests
from loguru import logger

from app.config import config
from app.models.schema import VideoAspect
from app.services import material
from app.utils import utils

KIE_BASE_URL = "https://api.kie.ai"
DEFAULT_MODEL = "bytedance/seedance-2-fast"
DEFAULT_MAX_CLIPS = 3
DEFAULT_POLL_TIMEOUT = 600
POLL_INTERVAL_SECONDS = 15

# 追加到关键词后的固定风格后缀，保证素材是干净的 b-roll。
PROMPT_SUFFIX = (
    ", cinematic b-roll footage, natural lighting, smooth camera movement, "
    "no text overlays, no captions, no watermarks"
)


def _get_api_key() -> str:
    return (
        str(config.app.get("kie_api_key", "") or "").strip()
        or os.environ.get("KIE_API_KEY", "").strip()
    )


def is_enabled() -> bool:
    return bool(_get_api_key())


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {_get_api_key()}",
        "Content-Type": "application/json",
    }


def _aspect_ratio_value(video_aspect) -> str:
    try:
        return VideoAspect(video_aspect).value
    except ValueError:
        return VideoAspect.portrait.value


def _tasks_file(task_id: str) -> str:
    return os.path.join(utils.task_dir(task_id), "kie_tasks.json")


def _persist_tasks(task_id: str, records: list) -> None:
    """提交后立即落盘 taskId，会话中断也能追回已扣费任务。"""
    try:
        with open(_tasks_file(task_id), "w", encoding="utf-8") as f:
            json.dump(records, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"failed to persist kie task ids: {e}")


def _submit_task(prompt: str, model: str, aspect_ratio: str, duration: int) -> str:
    payload = {
        "model": model,
        "input": {
            "prompt": prompt,
            "aspect_ratio": aspect_ratio,
            "duration": duration,
            "resolution": "720p",
            # 成片使用 TTS 配音和独立 BGM，素材自带音轨只会被丢弃。
            "generate_audio": False,
        },
    }
    response = requests.post(
        f"{KIE_BASE_URL}/api/v1/jobs/createTask",
        headers=_headers(),
        json=payload,
        verify=material._get_tls_verify(),
        timeout=(30, 120),
    )
    response.raise_for_status()
    body = response.json()
    if body.get("code") != 200:
        raise RuntimeError(f"kie createTask failed: {body.get('msg', body)}")
    return body["data"]["taskId"]


def _poll_task(kie_task_id: str) -> dict:
    response = requests.get(
        f"{KIE_BASE_URL}/api/v1/jobs/recordInfo",
        headers=_headers(),
        params={"taskId": kie_task_id},
        verify=material._get_tls_verify(),
        timeout=(30, 60),
    )
    response.raise_for_status()
    return response.json().get("data") or {}


def generate_videos(
    task_id: str,
    search_terms: list,
    video_aspect=VideoAspect.portrait,
    audio_duration: float = 0.0,
    max_clip_duration: int = 5,
) -> list:
    """为关键词生成 b-roll 素材并返回本地文件路径列表。失败返回 []。"""
    api_key = _get_api_key()
    if not api_key:
        logger.error("kie_api_key is not set (config or KIE_API_KEY env)")
        return []

    model = str(config.app.get("kie_video_model", "") or "").strip() or DEFAULT_MODEL
    try:
        max_clips = int(config.app.get("kie_max_clips", DEFAULT_MAX_CLIPS))
    except (TypeError, ValueError):
        max_clips = DEFAULT_MAX_CLIPS
    try:
        poll_timeout = int(config.app.get("kie_poll_timeout", DEFAULT_POLL_TIMEOUT))
    except (TypeError, ValueError):
        poll_timeout = DEFAULT_POLL_TIMEOUT

    aspect_ratio = _aspect_ratio_value(video_aspect)
    # seedance 支持 4-15 秒；拼接阶段会按 max_clip_duration 截取。
    duration = max(4, min(15, int(max_clip_duration or 5)))

    # 素材可循环复用，无需覆盖全部配音时长；按需求量与成本上限取小。
    if audio_duration > 0:
        needed = math.ceil(audio_duration / duration)
        clip_count = max(1, min(max_clips, needed))
    else:
        clip_count = max_clips
    terms = [str(t).strip() for t in search_terms if str(t).strip()][:clip_count]
    if not terms:
        logger.error("kie source: no search terms to generate from")
        return []

    logger.info(
        f"kie source: generating {len(terms)} clip(s) with {model}, "
        f"{aspect_ratio}, {duration}s each"
    )

    records = []
    for term in terms:
        prompt = f"{term}{PROMPT_SUFFIX}"
        try:
            kie_task_id = _submit_task(prompt, model, aspect_ratio, duration)
        except Exception as e:
            logger.error(
                "kie submit failed for "
                f"'{term}': {material._redact_request_error(e, api_key)}"
            )
            continue
        records.append(
            {
                "term": term,
                "model": model,
                "kie_task_id": kie_task_id,
                "state": "submitted",
            }
        )
        # 提交即扣费：每提交一个任务就立即落盘。
        _persist_tasks(task_id, records)

    if not records:
        return []

    deadline = time.time() + poll_timeout
    video_paths = []
    pending = {record["kie_task_id"]: record for record in records}
    while pending and time.time() < deadline:
        for kie_task_id, record in list(pending.items()):
            try:
                data = _poll_task(kie_task_id)
            except Exception as e:
                logger.warning(
                    "kie poll error for "
                    f"{kie_task_id}: {material._redact_request_error(e, api_key)}"
                )
                continue
            state = data.get("state", "")
            if state == "success":
                record["state"] = "success"
                result_json = data.get("resultJson") or "{}"
                if isinstance(result_json, str):
                    result_json = json.loads(result_json)
                for url in result_json.get("resultUrls") or []:
                    saved_path = material.save_video(
                        url, save_dir=utils.task_dir(task_id)
                    )
                    if saved_path:
                        video_paths.append(saved_path)
                del pending[kie_task_id]
            elif state == "fail":
                record["state"] = "fail"
                logger.error(
                    f"kie task {kie_task_id} ('{record['term']}') failed: "
                    f"{data.get('failMsg', 'unknown')}"
                )
                del pending[kie_task_id]
        _persist_tasks(task_id, records)
        if pending:
            time.sleep(POLL_INTERVAL_SECONDS)

    for record in pending.values():
        record["state"] = "timeout"
        logger.error(
            f"kie task {record['kie_task_id']} ('{record['term']}') still pending "
            f"after {poll_timeout}s; it may finish later — do NOT resubmit, "
            f"check kie_tasks.json"
        )
    if pending:
        _persist_tasks(task_id, records)

    logger.info(f"kie source: {len(video_paths)} clip(s) ready")
    return video_paths
