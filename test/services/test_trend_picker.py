import json
import unittest
from unittest.mock import MagicMock, patch

from app.services import trend_picker


def _response(json_payload=None, content=b"", status=200):
    mock = MagicMock()
    mock.status_code = status
    mock.json.return_value = json_payload
    mock.content = content
    mock.raise_for_status.return_value = None
    return mock


def _reddit_atom(titles):
    entries = "".join(
        f"<entry><title>{title}</title>"
        f'<link href="https://www.reddit.com/r/test/{i}"/></entry>'
        for i, title in enumerate(titles)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<feed xmlns="http://www.w3.org/2005/Atom">' + entries + "</feed>"
    ).encode("utf-8")


_TRENDS_RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss xmlns:ht="https://trends.google.com/trending/rss" version="2.0">
  <channel>
    <title>Trending Now</title>
    <item>
      <title>mortgage rates</title>
      <ht:approx_traffic>200,000+</ht:approx_traffic>
      <link>https://trends.google.com/item1</link>
    </item>
    <item>
      <title>celebrity gossip</title>
      <link>https://trends.google.com/item2</link>
    </item>
  </channel>
</rss>"""


def _llm_json(subject="The real cost of owning a pool", **overrides):
    data = {
        "subject": subject,
        "hook": "A backyard pool costs more per swim than a hotel stay.",
        "script_brief": "Break down purchase, maintenance and opportunity cost.",
        "why": "Signal 1 is rising fast.",
        "evidence_numbers": [1],
        "runner_ups": ["Why mortgage rates just moved"],
    }
    data.update(overrides)
    return json.dumps(data)


class TestFetchers(unittest.TestCase):
    @patch.object(trend_picker.requests, "get")
    def test_reddit_single_sub_failure_degrades(self, mock_get):
        ok = _response(content=_reddit_atom(["Is a heat pump worth it?"]))
        boom = _response()
        boom.raise_for_status.side_effect = RuntimeError("403")
        mock_get.side_effect = [boom, ok]

        candidates, errors = trend_picker.fetch_reddit_candidates(["dead", "alive"])

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["title"], "Is a heat pump worth it?")
        self.assertIn("reddit r/dead", errors[0])
        self.assertTrue(candidates[0]["url"].startswith("https://www.reddit.com/"))

    @patch.object(trend_picker.requests, "get")
    def test_reddit_empty_body_reports_rate_limit(self, mock_get):
        # Reddit 匿名限速超限时返回 200 空 body，必须报错而不是静默零候选。
        mock_get.return_value = _response(content=b"")

        candidates, errors = trend_picker.fetch_reddit_candidates(["sub"])

        self.assertEqual(candidates, [])
        self.assertIn("rate limited", errors[0])

    @patch.object(trend_picker.requests, "get")
    def test_google_trends_parses_rss_with_traffic(self, mock_get):
        mock_get.return_value = _response(content=_TRENDS_RSS)

        candidates, errors = trend_picker.fetch_google_trends_candidates("US")

        self.assertEqual(errors, [])
        self.assertEqual(len(candidates), 2)
        self.assertEqual(candidates[0]["title"], "mortgage rates")
        self.assertIn("200,000+", candidates[0]["signal"])
        self.assertEqual(candidates[1]["signal"], "trending now")

    @patch.object(trend_picker.requests, "get")
    def test_google_trends_failure_returns_error_not_raise(self, mock_get):
        mock_get.side_effect = RuntimeError("network down")

        candidates, errors = trend_picker.fetch_google_trends_candidates("US")

        self.assertEqual(candidates, [])
        self.assertEqual(len(errors), 1)
        self.assertIn("google trends", errors[0])


class TestGatherCandidates(unittest.TestCase):
    @patch.object(trend_picker, "fetch_google_trends_candidates")
    @patch.object(trend_picker, "fetch_reddit_candidates")
    def test_dedup_and_cap(self, mock_reddit, mock_trends):
        reddit = [
            {"title": "Mortgage Rates", "source": "r", "signal": "s", "url": ""},
            {"title": "unique reddit", "source": "r", "signal": "s", "url": ""},
        ]
        trends = [
            # 与 Reddit 候选仅大小写不同，应当被去重。
            {"title": "mortgage rates", "source": "g", "signal": "s", "url": ""},
            {"title": "unique trend", "source": "g", "signal": "s", "url": ""},
        ]
        mock_reddit.return_value = (reddit, [])
        mock_trends.return_value = (trends, ["one source warning"])
        settings = dict(
            subreddits=["x"],
            niches=["finance"],
            geo="US",
            target_duration_seconds=65,
            max_candidates=3,
        )

        candidates, errors = trend_picker.gather_candidates(settings)

        titles = [c["title"] for c in candidates]
        self.assertEqual(titles, ["Mortgage Rates", "unique reddit", "unique trend"])
        self.assertEqual(errors, ["one source warning"])


class TestPickBestBet(unittest.TestCase):
    def _settings(self):
        return dict(
            subreddits=["x"],
            niches=["personal finance"],
            geo="US",
            target_duration_seconds=65,
            max_candidates=40,
        )

    def _candidates(self):
        return [
            {
                "title": "mortgage rates",
                "source": "google trends (US)",
                "signal": "approx traffic 200,000+",
                "url": "https://trends.google.com/item1",
            },
            {
                "title": "Is a pool worth it?",
                "source": "reddit r/personalfinance (rising)",
                "signal": "score 900, 300 comments",
                "url": "https://www.reddit.com/r/pf/1",
            },
        ]

    def test_prompt_contains_signals_niche_and_duration(self):
        prompt = trend_picker.build_best_bet_prompt(
            self._candidates(), self._settings()
        )

        self.assertIn("1. mortgage rates", prompt)
        self.assertIn("2. Is a pool worth it?", prompt)
        self.assertIn("personal finance", prompt)
        self.assertIn("65 seconds", prompt)
        self.assertIn("faceless", prompt)

    @patch.object(trend_picker.llm, "_generate_response")
    @patch.object(trend_picker, "gather_candidates")
    def test_happy_path_maps_evidence(self, mock_gather, mock_llm):
        mock_gather.return_value = (self._candidates(), [])
        mock_llm.return_value = _llm_json()

        best = trend_picker.pick_best_bet()

        self.assertEqual(best["subject"], "The real cost of owning a pool")
        self.assertEqual(best["evidence"][0]["title"], "mortgage rates")
        self.assertEqual(best["candidate_count"], 2)
        self.assertEqual(best["source_errors"], [])
        self.assertEqual(best["target_duration_seconds"], 65)

    @patch.object(trend_picker.llm, "_generate_response")
    @patch.object(trend_picker, "gather_candidates")
    def test_llm_json_wrapped_in_fence_and_prose_still_parses(
        self, mock_gather, mock_llm
    ):
        mock_gather.return_value = (self._candidates(), [])
        mock_llm.return_value = (
            "Here is my pick:\n```json\n" + _llm_json() + "\n```\nGood luck!"
        )

        best = trend_picker.pick_best_bet()

        self.assertEqual(best["subject"], "The real cost of owning a pool")

    @patch.object(trend_picker.llm, "_generate_response")
    @patch.object(trend_picker, "gather_candidates")
    def test_llm_error_string_raises(self, mock_gather, mock_llm):
        mock_gather.return_value = (self._candidates(), [])
        mock_llm.return_value = "Error: all cli backends failed"

        with self.assertRaises(RuntimeError) as ctx:
            trend_picker.pick_best_bet()
        self.assertIn("all cli backends failed", str(ctx.exception))

    @patch.object(trend_picker.llm, "_generate_response")
    @patch.object(trend_picker, "gather_candidates")
    def test_unparseable_llm_response_raises(self, mock_gather, mock_llm):
        mock_gather.return_value = (self._candidates(), [])
        mock_llm.return_value = "I could not decide, sorry."

        with self.assertRaises(RuntimeError) as ctx:
            trend_picker.pick_best_bet()
        self.assertIn("could not parse", str(ctx.exception))

    @patch.object(trend_picker.llm, "_generate_response")
    @patch.object(trend_picker, "gather_candidates")
    def test_missing_subject_raises(self, mock_gather, mock_llm):
        mock_gather.return_value = (self._candidates(), [])
        mock_llm.return_value = _llm_json(subject="")

        with self.assertRaises(RuntimeError):
            trend_picker.pick_best_bet()

    @patch.object(trend_picker, "gather_candidates")
    def test_no_signals_at_all_raises_with_source_errors(self, mock_gather):
        mock_gather.return_value = ([], ["reddit r/x: 403", "google trends: down"])

        with self.assertRaises(RuntimeError) as ctx:
            trend_picker.pick_best_bet()
        self.assertIn("reddit r/x: 403", str(ctx.exception))

    @patch.object(trend_picker.llm, "_generate_response")
    @patch.object(trend_picker, "gather_candidates")
    def test_invalid_evidence_numbers_ignored(self, mock_gather, mock_llm):
        mock_gather.return_value = (self._candidates(), [])
        mock_llm.return_value = _llm_json(evidence_numbers=[0, 99, "two", 2])

        best = trend_picker.pick_best_bet()

        self.assertEqual(len(best["evidence"]), 1)
        self.assertEqual(best["evidence"][0]["title"], "Is a pool worth it?")


if __name__ == "__main__":
    unittest.main()
