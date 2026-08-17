from uuid import uuid4

from fastapi import Request

from app.config import config
from app.models.exception import HttpException


def get_task_id(request: Request):
    task_id = request.headers.get("x-task-id")
    if not task_id:
        task_id = uuid4()
    return str(task_id)


def get_api_key(request: Request):
    api_key = request.headers.get("x-api-key")
    return api_key


def verify_token(request: Request):
    # 鉴权策略：配置了 api_key 就强制校验；没配置则放行。服务只绑定
    # 127.0.0.1 且 CORS 默认关闭，本机单用户场景下空配置仍可用；配置键后
    # 浏览器盲发的跨源简单请求（如 multipart 表单）也会被 401 挡住。
    configured_key = config.app.get("api_key", "")
    if not configured_key:
        return None
    token = get_api_key(request)
    if token != configured_key:
        request_id = get_task_id(request)
        request_url = request.url
        user_agent = request.headers.get("user-agent")
        raise HttpException(
            task_id=request_id,
            status_code=401,
            message=f"invalid token: {request_url}, {user_agent}",
        )
