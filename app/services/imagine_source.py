"""Generate b-roll clips with Grok Imagine via the xAI video API.

每个脚本关键词提交一个文生视频任务（默认 grok-imagine-video-1.5），
轮询完成后下载为本地素材，返回与 material.download_videos 相同的路径列表。

鉴权优先用 imagine_api_key / XAI_API_KEY / grok_api_key；都没有时复用
本机 `grok` CLI 登录（~/.grok/auth.json）。费用在提交瞬间扣除，所以
request_id 必须在提交后立即落盘，且绝不重复提交。
"""

import json
import math
import os
import shutil
import subprocess
import tempfile
import time

import requests
from loguru import logger

from app.config import config
from app.models.schema import VideoAspect
from app.services import material
from app.utils import utils

IMAGINE_BASE_URL = "https://api.x.ai/v1"
DEFAULT_MODEL = "grok-imagine-video-1.5"
DEFAULT_MAX_CLIPS = 3
DEFAULT_POLL_TIMEOUT = 600
POLL_INTERVAL_SECONDS = 5
DEFAULT_RESOLUTION = "480p"
ALLOWED_RESOLUTIONS = ("480p", "720p", "1080p")
MIN_DURATION = 1
MAX_DURATION = 15

PROMPT_SUFFIX = (
    ", cinematic b-roll footage, natural lighting, smooth camera movement, "
    "no text overlays, no captions, no watermarks"
)


def _configured_api_key() -> str:
    return (
        str(config.app.get("imagine_api_key", "") or "").strip()
        or os.environ.get("XAI_API_KEY", "").strip()
        or str(config.app.get("grok_api_key", "") or "").strip()
    )


def _auth_path() -> str:
    home = os.environ.get("GROK_HOME") or os.path.expanduser("~/.grok")
    return os.path.join(home, "auth.json")


def _cli_session_token() -> str:
    path = _auth_path()
    if not os.path.isfile(path):
        return ""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        logger.warning(f"failed to read grok CLI auth file: {e}")
        return ""
    if not isinstance(data, dict):
        return ""

    best_token = ""
    best_expiry = ""
    for value in data.values():
        if not isinstance(value, dict):
            continue
        token = str(value.get("key") or "").strip()
        if not token:
            continue
        expiry = str(value.get("expires_at") or "")
        if not best_token or expiry > best_expiry:
            best_token = token
            best_expiry = expiry
    return best_token


def _resolve_grok_binary() -> str:
    found = shutil.which("grok")
    if found:
        return found
    for candidate in ("~/.grok/bin/grok", "~/.local/bin/grok"):
        expanded = os.path.expanduser(candidate)
        if os.path.isfile(expanded) and os.access(expanded, os.X_OK):
            return expanded
    raise FileNotFoundError("grok CLI not found on PATH or in ~/.grok/bin")


def _refresh_cli_session() -> None:
    """让 grok CLI 自己刷新 OIDC token，避免我们改写 auth.json。"""
    try:
        binary = _resolve_grok_binary()
    except FileNotFoundError as e:
        logger.warning(f"cannot refresh grok CLI session: {e}")
        return
    env = dict(os.environ)
    env.update({"NO_COLOR": "1", "CLICOLOR": "0", "TERM": "dumb"})
    try:
        subprocess.run(
            [binary, "models"],
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
            cwd=tempfile.gettempdir(),
        )
    except Exception as e:
        logger.warning(f"grok CLI session refresh failed: {e}")


def _get_bearer() -> str:
    return _configured_api_key() or _cli_session_token()


def is_enabled() -> bool:
    return bool(_configured_api_key() or _cli_session_token())


def _headers(bearer: str) -> dict:
    return {
        "Authorization": f"Bearer {bearer}",
        "Content-Type": "application/json",
    }


def _aspect_ratio_value(video_aspect) -> str:
    try:
        return VideoAspect(video_aspect).value
    except ValueError:
        return VideoAspect.portrait.value


def _tasks_file(task_id: str) -> str:
    return os.path.join(utils.task_dir(task_id), "imagine_tasks.json")


def _persist_tasks(task_id: str, records: list) -> None:
    """提交后立即落盘 request_id，会话中断也能追回已扣费任务。"""
    try:
        with open(_tasks_file(task_id), "w", encoding="utf-8") as f:
            json.dump(records, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"failed to persist imagine request ids: {e}")


def _get_resolution() -> str:
    resolution = str(config.app.get("imagine_resolution", "") or "").strip().lower()
    if not resolution:
        return DEFAULT_RESOLUTION
    if resolution not in ALLOWED_RESOLUTIONS:
        logger.warning(
            f"imagine_resolution '{resolution}' is not one of {ALLOWED_RESOLUTIONS}, "
            f"falling back to {DEFAULT_RESOLUTION}"
        )
        return DEFAULT_RESOLUTION
    return resolution


def _submit_task(
    prompt: str,
    model: str,
    aspect_ratio: str,
    duration: int,
    resolution: str,
    bearer: str,
) -> tuple[str, str]:
    payload = {
        "model": model,
        "prompt": prompt,
        "duration": duration,
        "aspect_ratio": aspect_ratio,
        "resolution": resolution,
    }
    response = requests.post(
        f"{IMAGINE_BASE_URL}/videos/generations",
        headers=_headers(bearer),
        json=payload,
        verify=material._get_tls_verify(),
        timeout=(30, 120),
    )
    if response.status_code == 401:
        _refresh_cli_session()
        bearer = _get_bearer()
        response = requests.post(
            f"{IMAGINE_BASE_URL}/videos/generations",
            headers=_headers(bearer),
            json=payload,
            verify=material._get_tls_verify(),
            timeout=(30, 120),
        )
    response.raise_for_status()
    body = response.json()
    request_id = str(body.get("request_id") or "").strip()
    if not request_id:
        raise RuntimeError(f"imagine create failed: {body}")
    return request_id, bearer


def _poll_task(request_id: str, bearer: str) -> dict:
    response = requests.get(
        f"{IMAGINE_BASE_URL}/videos/{request_id}",
        headers={"Authorization": f"Bearer {bearer}"},
        verify=material._get_tls_verify(),
        timeout=(30, 60),
    )
    response.raise_for_status()
    return response.json() or {}


def generate_videos(
    task_id: str,
    search_terms: list,
    video_aspect=VideoAspect.portrait,
    audio_duration: float = 0.0,
    max_clip_duration: int = 5,
) -> list:
    """为关键词生成 b-roll 素材并返回本地文件路径列表。失败返回 []。"""
    bearer = _get_bearer()
    if not bearer:
        logger.error(
            "imagine source: no auth (set imagine_api_key / XAI_API_KEY, "
            "or sign in with `grok login`)"
        )
        return []

    model = (
        str(config.app.get("imagine_video_model", "") or "").strip() or DEFAULT_MODEL
    )
    try:
        max_clips = int(config.app.get("imagine_max_clips", DEFAULT_MAX_CLIPS))
    except (TypeError, ValueError):
        max_clips = DEFAULT_MAX_CLIPS
    try:
        poll_timeout = int(config.app.get("imagine_poll_timeout", DEFAULT_POLL_TIMEOUT))
    except (TypeError, ValueError):
        poll_timeout = DEFAULT_POLL_TIMEOUT

    aspect_ratio = _aspect_ratio_value(video_aspect)
    resolution = _get_resolution()
    duration = max(MIN_DURATION, min(MAX_DURATION, int(max_clip_duration or 5)))

    if audio_duration > 0:
        needed = math.ceil(audio_duration / duration)
        clip_count = max(1, min(max_clips, needed))
    else:
        clip_count = max_clips
    terms = [str(t).strip() for t in search_terms if str(t).strip()][:clip_count]
    if not terms:
        logger.error("imagine source: no search terms to generate from")
        return []

    logger.info(
        f"imagine source: generating {len(terms)} clip(s) with {model}, "
        f"{aspect_ratio}, {duration}s each, {resolution}"
    )

    records = []
    for term in terms:
        prompt = f"{term}{PROMPT_SUFFIX}"
        try:
            request_id, bearer = _submit_task(
                prompt, model, aspect_ratio, duration, resolution, bearer
            )
        except Exception as e:
            logger.error(
                "imagine submit failed for "
                f"'{term}': {material._redact_request_error(e, bearer)}"
            )
            continue
        records.append(
            {
                "term": term,
                "model": model,
                "request_id": request_id,
                "state": "submitted",
            }
        )
        _persist_tasks(task_id, records)

    if not records:
        return []

    deadline = time.time() + poll_timeout
    video_paths = []
    pending = {record["request_id"]: record for record in records}
    while pending and time.time() < deadline:
        for request_id, record in list(pending.items()):
            try:
                data = _poll_task(request_id, bearer)
            except Exception as e:
                logger.warning(
                    "imagine poll error for "
                    f"{request_id}: {material._redact_request_error(e, bearer)}"
                )
                continue
            status = str(data.get("status") or "")
            if status == "done":
                record["state"] = "success"
                url = (
                    (data.get("video") or {})
                    if isinstance(data.get("video"), dict)
                    else {}
                ).get("url")
                if url:
                    saved_path = material.save_video(
                        url, save_dir=utils.task_dir(task_id)
                    )
                    if saved_path:
                        video_paths.append(saved_path)
                del pending[request_id]
            elif status in {"failed", "expired"}:
                record["state"] = status
                error = data.get("error") or {}
                message = (
                    error.get("message")
                    if isinstance(error, dict)
                    else data.get("error") or status
                )
                logger.error(
                    f"imagine task {request_id} ('{record['term']}') {status}: "
                    f"{message}"
                )
                del pending[request_id]
        _persist_tasks(task_id, records)
        if pending:
            time.sleep(POLL_INTERVAL_SECONDS)

    for record in pending.values():
        record["state"] = "timeout"
        logger.error(
            f"imagine task {record['request_id']} ('{record['term']}') still pending "
            f"after {poll_timeout}s; it may finish later — do NOT resubmit, "
            f"check imagine_tasks.json"
        )
    if pending:
        _persist_tasks(task_id, records)

    logger.info(f"imagine source: {len(video_paths)} clip(s) ready")
    return video_paths
