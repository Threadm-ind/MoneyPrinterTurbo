"""Best-bet topic picker: live trend signals -> LLM-scored video subject.

拉取 Reddit rising 和 Google Trends RSS 两路免费信号，交给现有 LLM 链路
（cli_agent 订阅 CLI，零 API 成本）按 趋势强度 × 无人出镜可执行性 × 财经
利基契合度 打分，返回一个可直接生成的最佳选题。两路信号互相独立降级：
一路失败不影响另一路，全部失败才抛错。

不做任何"保证爆款"承诺：产出是"当日证据支持的最佳赌注"，evidence 字段
保留原始信号供人工复核。
"""

import json
import xml.etree.ElementTree as ET

import requests
from loguru import logger

from app.config import config
from app.services import llm

# Reddit 要求描述性 UA；通用浏览器 UA 反而更容易被拦。
_HTTP_HEADERS = {"User-Agent": "linux:moneyprinterturbo-best-bet:v1.0"}
_ATOM_NAMESPACE = "{http://www.w3.org/2005/Atom}"

DEFAULT_SUBREDDITS = (
    "personalfinance",
    "UKPersonalFinance",
    "investing",
    "FluentInFinance",
    "povertyfinance",
)
DEFAULT_NICHES = (
    "personal finance and money facts",
    "the economics of owning expensive things",
)
DEFAULT_GEO = "US"
# Creator Rewards 只对 >1 分钟的视频付费，留 5 秒余量。
DEFAULT_TARGET_DURATION_SECONDS = 65
DEFAULT_MAX_CANDIDATES = 40
_REQUEST_TIMEOUT_SECONDS = 15
_TRENDS_RSS_URL = "https://trends.google.com/trending/rss"
_HT_NAMESPACE = "{https://trends.google.com/trending/rss}"


def _settings():
    cfg = getattr(config, "trend_picker", None) or {}
    return {
        "subreddits": list(cfg.get("subreddits") or DEFAULT_SUBREDDITS),
        "niches": list(cfg.get("niches") or DEFAULT_NICHES),
        "geo": str(cfg.get("geo") or DEFAULT_GEO),
        "target_duration_seconds": int(
            cfg.get("target_duration_seconds") or DEFAULT_TARGET_DURATION_SECONDS
        ),
        "max_candidates": int(cfg.get("max_candidates") or DEFAULT_MAX_CANDIDATES),
    }


def fetch_reddit_candidates(subreddits, per_sub_limit=10, timeout=None):
    """逐个子版拉取 rising 帖子；单个子版失败只记 warning，不中断整体。

    走 RSS（Atom）而不是 rising.json：Reddit 对无 OAuth 的 JSON 接口按 IP
    封 403，RSS 通道仍开放，但匿名限速约 10 次/分钟——超限返回 200 空 body，
    这里把空 body 当错误上报而不是静默返回零候选。
    """
    timeout = timeout or _REQUEST_TIMEOUT_SECONDS
    candidates, errors = [], []
    for sub in subreddits:
        url = f"https://www.reddit.com/r/{sub}/rising/.rss"
        try:
            resp = requests.get(
                url,
                params={"limit": per_sub_limit},
                headers=_HTTP_HEADERS,
                timeout=timeout,
            )
            resp.raise_for_status()
            if not resp.content:
                raise ValueError("empty response (likely rate limited)")
            root = ET.fromstring(resp.content)
        except Exception as e:
            errors.append(f"reddit r/{sub}: {e}")
            logger.warning(f"trend picker: reddit r/{sub} failed: {e}")
            continue
        for entry in root.iter(f"{_ATOM_NAMESPACE}entry"):
            title = (entry.findtext(f"{_ATOM_NAMESPACE}title") or "").strip()
            if not title:
                continue
            link = entry.find(f"{_ATOM_NAMESPACE}link")
            candidates.append(
                {
                    "title": title,
                    "source": f"reddit r/{sub} (rising)",
                    "signal": "rising post",
                    "url": link.get("href", "") if link is not None else "",
                }
            )
    return candidates, errors


def fetch_google_trends_candidates(geo=None, timeout=None):
    """Google Trends 官方 RSS：免 key、稳定，比逆向内部接口可靠得多。"""
    timeout = timeout or _REQUEST_TIMEOUT_SECONDS
    geo = geo or DEFAULT_GEO
    candidates, errors = [], []
    try:
        resp = requests.get(
            _TRENDS_RSS_URL,
            params={"geo": geo},
            headers=_HTTP_HEADERS,
            timeout=timeout,
        )
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
    except Exception as e:
        errors.append(f"google trends ({geo}): {e}")
        logger.warning(f"trend picker: google trends rss failed: {e}")
        return candidates, errors
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        if not title:
            continue
        traffic = (item.findtext(f"{_HT_NAMESPACE}approx_traffic") or "").strip()
        candidates.append(
            {
                "title": title,
                "source": f"google trends ({geo})",
                "signal": f"approx traffic {traffic}" if traffic else "trending now",
                "url": (item.findtext("link") or "").strip(),
            }
        )
    return candidates, errors


def gather_candidates(settings=None):
    settings = settings or _settings()
    reddit, reddit_errors = fetch_reddit_candidates(settings["subreddits"])
    trends, trends_errors = fetch_google_trends_candidates(settings["geo"])
    # 信号去重（同一话题常同时出现在多个子版），保持先到先得的顺序。
    seen, candidates = set(), []
    for candidate in reddit + trends:
        key = candidate["title"].lower()
        if key in seen:
            continue
        seen.add(key)
        candidates.append(candidate)
    return candidates[: settings["max_candidates"]], reddit_errors + trends_errors


def build_best_bet_prompt(candidates, settings=None):
    settings = settings or _settings()
    lines = [
        f"{i + 1}. {c['title']} [{c['source']}; {c['signal']}]"
        for i, c in enumerate(candidates)
    ]
    niches = "; ".join(settings["niches"])
    duration = settings["target_duration_seconds"]
    return f"""# Role: Short-Video Topic Strategist

Below are live trending signals gathered just now (Reddit rising posts and Google
trending searches). Pick the SINGLE best topic for a faceless TikTok video and
reframe it as a concrete video subject.

# Constraints
- Channel niche: {niches}.
- Format: faceless narration over AI-generated b-roll. No dances, no reactions,
  no celebrity footage, no content that needs a human on camera.
- The narration must comfortably fill at least {duration} seconds.
- Prefer topics with clear money/economics angles and durable curiosity, not
  breaking news that will be stale in a day.
- If a raw signal is off-niche, you may reframe it into the niche (e.g. a
  trending product -> "the real cost of owning X"), but only when the link is
  genuine. Never force a connection.

# Output
Return ONLY a JSON object, no markdown fence, with exactly these keys:
- "subject": the video subject line to generate the video from (<= 120 chars)
- "hook": the opening line for the script (one sentence)
- "script_brief": 2-3 sentences of direction for the scriptwriter
- "why": why this is today's best bet, citing the signals by their numbers
- "evidence_numbers": array of the signal numbers (integers) that support it
- "runner_ups": array of up to 2 alternative subject lines

# Live signals
{chr(10).join(lines)}
""".strip()


def _parse_best_bet_response(response, candidates):
    text = llm._strip_code_fence(str(response or "").strip())
    # CLI 后端偶尔在 JSON 前后加说明文字，截取最外层大括号兜底。
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        text = text[start : end + 1]
    data = json.loads(text)
    subject = str(data.get("subject") or "").strip()
    if not subject:
        raise ValueError("llm response is missing 'subject'")
    evidence = []
    for number in data.get("evidence_numbers") or []:
        try:
            index = int(number) - 1
        except (TypeError, ValueError):
            continue
        if 0 <= index < len(candidates):
            evidence.append(candidates[index])
    return {
        "subject": subject,
        "hook": str(data.get("hook") or "").strip(),
        "script_brief": str(data.get("script_brief") or "").strip(),
        "why": str(data.get("why") or "").strip(),
        "evidence": evidence,
        "runner_ups": [str(r).strip() for r in data.get("runner_ups") or [] if r],
    }


def pick_best_bet(app_config=None):
    """返回今日最佳选题；两路信号全灭或 LLM 失败时抛 RuntimeError。"""
    settings = _settings()
    candidates, source_errors = gather_candidates(settings)
    if not candidates:
        raise RuntimeError(
            "no trend signals available: " + "; ".join(source_errors or ["unknown"])
        )
    prompt = build_best_bet_prompt(candidates, settings)
    response = llm._generate_response(prompt, app_config)
    # llm 模块的约定是把异常吞成 "Error: ..." 字符串，这里还原成异常。
    if response.startswith("Error: "):
        raise RuntimeError(response)
    try:
        best = _parse_best_bet_response(response, candidates)
    except (ValueError, json.JSONDecodeError) as e:
        raise RuntimeError(f"could not parse best-bet response: {e}") from e
    best["source_errors"] = source_errors
    best["candidate_count"] = len(candidates)
    best["target_duration_seconds"] = settings["target_duration_seconds"]
    logger.success(
        f"best bet picked from {len(candidates)} live signals: {best['subject']}"
    )
    return best
