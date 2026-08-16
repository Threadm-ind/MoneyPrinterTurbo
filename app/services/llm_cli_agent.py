"""Adapter that generates text by shelling out to local coding-agent CLIs.

支持 claude / grok / kimi 等本地 CLI 订阅计划，无需 API Key。所有子进程
调用都带硬超时：任务管理器只有一个并发槽位，挂起的 CLI 会永久阻塞它。
"""

import os
import re
import shutil
import subprocess
import tempfile

from loguru import logger

from app.config import config

# 每个后端的 argv 模板。prompt 体积可能很大（generate_terms 会带上整个
# 视频脚本），claude 支持 stdin 传入；grok/kimi 未提供 stdin 模式，prompt
# 作为单个 argv 元素传入（不经过 shell，无注入风险，且远小于 ARG_MAX）。
KNOWN_BACKENDS = {
    "claude": {
        "argv": ("-p", "--output-format", "text"),
        "stdin_prompt": True,
        "fallback_paths": ("~/.local/bin/claude",),
    },
    "grok": {
        "argv": ("-p", "{prompt}", "--output-format", "plain"),
        "stdin_prompt": False,
        "fallback_paths": ("~/.grok/bin/grok",),
    },
    "kimi": {
        "argv": ("-p", "{prompt}", "--output-format", "text"),
        "stdin_prompt": False,
        "fallback_paths": ("~/.kimi-code/bin/kimi",),
    },
}

DEFAULT_BACKEND = "claude"
DEFAULT_TIMEOUT_SECONDS = 90
# 主后端 + 最多两个回退，保证最坏情况约为 3 × timeout。
MAX_BACKEND_CHAIN = 3

_ANSI_CSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_ANSI_OSC_RE = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_ANSI_SINGLE_RE = re.compile(r"\x1b[@-Z\\-_]")


def _resolve_binary(backend: str) -> str:
    """定位 CLI 可执行文件。

    交互 shell 里 `claude`/`kimi` 往往是 shell function，子进程看不到；
    webui.sh / systemd 的 PATH 也可能不含用户目录，所以 which 失败后再
    尝试各 CLI 的固定安装路径。
    """
    spec = KNOWN_BACKENDS[backend]
    found = shutil.which(backend)
    if found:
        return found
    for candidate in spec["fallback_paths"]:
        expanded = os.path.expanduser(candidate)
        if os.path.isfile(expanded) and os.access(expanded, os.X_OK):
            return expanded
    raise FileNotFoundError(
        f"cli backend '{backend}' not found on PATH or in {spec['fallback_paths']}"
    )


def _clean_cli_output(text: str) -> str:
    text = _ANSI_OSC_RE.sub("", text)
    text = _ANSI_CSI_RE.sub("", text)
    text = _ANSI_SINGLE_RE.sub("", text)
    text = text.replace("\r", "")
    # kimi 的 -p 输出会给每行加 "• " 装饰前缀，脚本正文里不应保留。
    text = re.sub(r"^[•●]\s?", "", text, flags=re.MULTILINE)
    return text.strip()


def _build_env() -> dict:
    env = dict(os.environ)
    # 关闭彩色输出与本机的 verify-on-stop 钩子，保证 stdout 只有正文。
    env.update(
        {
            "NO_COLOR": "1",
            "CLICOLOR": "0",
            "TERM": "dumb",
            "VERIFY_ON_STOP": "0",
        }
    )
    return env


def _invoke_backend(backend: str, prompt: str, timeout: int) -> str:
    spec = KNOWN_BACKENDS[backend]
    binary = _resolve_binary(backend)
    argv = [binary]
    for arg in spec["argv"]:
        argv.append(prompt if arg == "{prompt}" else arg)
    stdin_text = prompt if spec["stdin_prompt"] else None

    logger.info(f"invoking cli backend '{backend}' ({binary}), timeout={timeout}s")
    result = subprocess.run(
        argv,
        input=stdin_text,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=_build_env(),
        # 中性目录：避免 CLI 加载本仓库的项目级配置（CLAUDE.md、MCP、hooks），
        # 既拖慢冷启动，也可能污染输出。
        cwd=tempfile.gettempdir(),
    )
    if result.returncode != 0:
        stderr_tail = _clean_cli_output(result.stderr or "")[-500:]
        raise RuntimeError(
            f"cli backend '{backend}' exited with code {result.returncode}: {stderr_tail}"
        )
    content = _clean_cli_output(result.stdout or "")
    if not content:
        raise RuntimeError(f"cli backend '{backend}' returned empty output")
    return content


def generate_via_cli(prompt: str, extra_values: dict) -> str:
    """按配置的后端顺序尝试生成，全部失败时抛出异常。

    调用方是 llm.py 的 `_generate_response`，其外层 except 会把异常转成
    "Error: ..." 字符串，符合该模块从不抛出的约定。
    """
    if config.is_running_in_container():
        raise RuntimeError(
            "CLI agents are not available inside Docker; run the app natively "
            "or choose another LLM provider."
        )

    command = str(extra_values.get("command") or DEFAULT_BACKEND).strip().lower()
    raw_fallbacks = str(extra_values.get("fallback_backends") or "")
    try:
        timeout = int(float(extra_values.get("timeout_seconds") or 0)) or (
            DEFAULT_TIMEOUT_SECONDS
        )
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT_SECONDS

    chain = [command]
    for name in raw_fallbacks.split(","):
        name = name.strip().lower()
        if name and name not in chain:
            chain.append(name)
    chain = chain[:MAX_BACKEND_CHAIN]

    errors = []
    for backend in chain:
        if backend not in KNOWN_BACKENDS:
            errors.append(f"{backend}: unknown cli backend")
            continue
        try:
            content = _invoke_backend(backend, prompt, timeout)
            logger.success(f"cli backend '{backend}' answered")
            return content
        except subprocess.TimeoutExpired:
            errors.append(f"{backend}: timed out after {timeout}s")
            logger.warning(f"cli backend '{backend}' timed out after {timeout}s")
        except Exception as e:
            errors.append(f"{backend}: {e}")
            logger.warning(f"cli backend '{backend}' failed: {e}")

    raise RuntimeError("all cli backends failed: " + "; ".join(errors))
