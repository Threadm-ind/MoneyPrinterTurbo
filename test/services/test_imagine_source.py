import unittest
from unittest.mock import MagicMock, patch

from app.services import imagine_source
from app.models.schema import VideoAspect


def _response(payload, status=200):
    mock = MagicMock()
    mock.status_code = status
    mock.json.return_value = payload
    mock.raise_for_status.return_value = None
    return mock


def _submit_ok(request_id="imagine-req-1"):
    return _response({"request_id": request_id})


def _poll_done(url="https://vidgen.x.ai/clip.mp4"):
    return _response(
        {
            "status": "done",
            "model": "grok-imagine-video-1.5",
            "video": {"url": url, "duration": 5},
        }
    )


def _poll_failed(message="boom"):
    return _response(
        {
            "status": "failed",
            "error": {"code": "invalid_argument", "message": message},
        }
    )


@patch.object(imagine_source, "_get_bearer", return_value="test-token")
@patch.object(imagine_source.material, "_get_tls_verify", return_value=True)
@patch.object(imagine_source.utils, "task_dir", return_value="/tmp/imagine-test-task")
@patch.object(imagine_source, "_persist_tasks")
@patch.object(imagine_source.time, "sleep")
class TestImagineGenerateVideos(unittest.TestCase):
    def test_happy_path_submits_polls_downloads(
        self, mock_sleep, mock_persist, mock_dir, mock_tls, mock_bearer
    ):
        with (
            patch.object(
                imagine_source.requests, "post", return_value=_submit_ok()
            ) as mock_post,
            patch.object(
                imagine_source.requests,
                "get",
                return_value=_poll_done(),
            ),
            patch.object(
                imagine_source.material, "save_video", return_value="/tmp/clip.mp4"
            ) as mock_save,
        ):
            result = imagine_source.generate_videos(
                "task-1", ["ocean waves"], VideoAspect.portrait, 10.0, 5
            )
        self.assertEqual(result, ["/tmp/clip.mp4"])
        payload = mock_post.call_args.kwargs["json"]
        self.assertEqual(payload["aspect_ratio"], "9:16")
        self.assertEqual(payload["duration"], 5)
        self.assertEqual(payload["model"], "grok-imagine-video-1.5")
        self.assertIn("ocean waves", payload["prompt"])
        headers = mock_post.call_args.kwargs["headers"]
        self.assertEqual(headers["Authorization"], "Bearer test-token")
        mock_save.assert_called_once()
        self.assertGreaterEqual(mock_persist.call_count, 1)
        first_records = mock_persist.call_args_list[0].args[1]
        self.assertEqual(first_records[0]["request_id"], "imagine-req-1")

    def test_max_clips_cap_respected(
        self, mock_sleep, mock_persist, mock_dir, mock_tls, mock_bearer
    ):
        terms = [f"term {i}" for i in range(10)]
        with (
            patch.object(
                imagine_source.config.app,
                "get",
                side_effect=lambda k, d=None: {
                    "imagine_max_clips": 2,
                    "imagine_video_model": "",
                    "imagine_poll_timeout": 600,
                }.get(k, d),
            ),
            patch.object(
                imagine_source.requests, "post", return_value=_submit_ok()
            ) as mock_post,
            patch.object(imagine_source.requests, "get", return_value=_poll_done()),
            patch.object(
                imagine_source.material, "save_video", return_value="/tmp/x.mp4"
            ),
        ):
            imagine_source.generate_videos(
                "task-1", terms, VideoAspect.portrait, 300.0, 5
            )
        self.assertEqual(mock_post.call_count, 2)

    def test_fail_state_returns_empty(
        self, mock_sleep, mock_persist, mock_dir, mock_tls, mock_bearer
    ):
        with (
            patch.object(imagine_source.requests, "post", return_value=_submit_ok()),
            patch.object(imagine_source.requests, "get", return_value=_poll_failed()),
        ):
            result = imagine_source.generate_videos(
                "task-1", ["ocean"], VideoAspect.portrait, 10.0, 5
            )
        self.assertEqual(result, [])

    def test_submit_error_does_not_block_other_terms(
        self, mock_sleep, mock_persist, mock_dir, mock_tls, mock_bearer
    ):
        responses = [RuntimeError("http 500"), _submit_ok("imagine-req-2")]

        def post_side_effect(*args, **kwargs):
            item = responses.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        with (
            patch.object(imagine_source.requests, "post", side_effect=post_side_effect),
            patch.object(imagine_source.requests, "get", return_value=_poll_done()),
            patch.object(
                imagine_source.material, "save_video", return_value="/tmp/x.mp4"
            ),
            patch.object(
                imagine_source.config.app,
                "get",
                side_effect=lambda k, d=None: {"imagine_max_clips": 5}.get(k, d),
            ),
        ):
            result = imagine_source.generate_videos(
                "task-1", ["a", "b"], VideoAspect.portrait, 60.0, 5
            )
        self.assertEqual(result, ["/tmp/x.mp4"])

    def test_duration_clamped_to_imagine_range(
        self, mock_sleep, mock_persist, mock_dir, mock_tls, mock_bearer
    ):
        with (
            patch.object(
                imagine_source.requests, "post", return_value=_submit_ok()
            ) as mock_post,
            patch.object(imagine_source.requests, "get", return_value=_poll_done()),
            patch.object(
                imagine_source.material, "save_video", return_value="/tmp/x.mp4"
            ),
        ):
            imagine_source.generate_videos(
                "task-1", ["a"], VideoAspect.portrait, 5.0, 20
            )
        self.assertEqual(mock_post.call_args.kwargs["json"]["duration"], 15)

    def test_401_refreshes_cli_session_and_retries_submit(
        self, mock_sleep, mock_persist, mock_dir, mock_tls, mock_bearer
    ):
        mock_bearer.side_effect = ["stale-token", "fresh-token"]
        posts = [
            _response({"error": "unauthorized"}, status=401),
            _submit_ok("imagine-req-retry"),
        ]

        with (
            patch.object(
                imagine_source.requests, "post", side_effect=posts
            ) as mock_post,
            patch.object(imagine_source.requests, "get", return_value=_poll_done()),
            patch.object(
                imagine_source.material, "save_video", return_value="/tmp/x.mp4"
            ),
            patch.object(imagine_source, "_refresh_cli_session") as mock_refresh,
        ):
            result = imagine_source.generate_videos(
                "task-1", ["ocean"], VideoAspect.portrait, 5.0, 5
            )
        self.assertEqual(result, ["/tmp/x.mp4"])
        mock_refresh.assert_called_once()
        self.assertEqual(mock_post.call_count, 2)
        self.assertEqual(
            mock_post.call_args_list[1].kwargs["headers"]["Authorization"],
            "Bearer fresh-token",
        )


class TestImagineResolution(unittest.TestCase):
    def _submit_with_resolution(self, configured):
        with (
            patch.object(imagine_source, "_get_bearer", return_value="test-token"),
            patch.object(imagine_source.material, "_get_tls_verify", return_value=True),
            patch.object(
                imagine_source.utils, "task_dir", return_value="/tmp/imagine-test-task"
            ),
            patch.object(imagine_source, "_persist_tasks"),
            patch.object(imagine_source.time, "sleep"),
            patch.object(
                imagine_source.config.app,
                "get",
                side_effect=lambda k, d=None: {"imagine_resolution": configured}.get(
                    k, d
                ),
            ),
            patch.object(
                imagine_source.requests, "post", return_value=_submit_ok()
            ) as mock_post,
            patch.object(imagine_source.requests, "get", return_value=_poll_done()),
            patch.object(
                imagine_source.material, "save_video", return_value="/tmp/x.mp4"
            ),
        ):
            imagine_source.generate_videos(
                "task-1", ["ocean"], VideoAspect.portrait, 5.0, 5
            )
        return mock_post.call_args.kwargs["json"]["resolution"]

    def test_default_is_480p(self):
        self.assertEqual(self._submit_with_resolution(""), "480p")

    def test_720p_passthrough(self):
        self.assertEqual(self._submit_with_resolution("720p"), "720p")

    def test_invalid_value_falls_back_to_default(self):
        self.assertEqual(self._submit_with_resolution("4k"), "480p")


class TestImagineEnablement(unittest.TestCase):
    def test_missing_auth_returns_empty(self):
        with patch.object(imagine_source, "_get_bearer", return_value=""):
            result = imagine_source.generate_videos("task-1", ["ocean"])
        self.assertEqual(result, [])

    def test_is_enabled_with_api_key(self):
        with patch.object(imagine_source, "_configured_api_key", return_value="xai-k"):
            self.assertTrue(imagine_source.is_enabled())

    def test_is_enabled_with_cli_session(self):
        with (
            patch.object(imagine_source, "_configured_api_key", return_value=""),
            patch.object(imagine_source, "_cli_session_token", return_value="jwt"),
        ):
            self.assertTrue(imagine_source.is_enabled())

    def test_is_enabled_false_without_key_or_cli(self):
        with (
            patch.object(imagine_source, "_configured_api_key", return_value=""),
            patch.object(imagine_source, "_cli_session_token", return_value=""),
        ):
            self.assertFalse(imagine_source.is_enabled())

    def test_bearer_prefers_config_key_over_cli_session(self):
        with (
            patch.object(imagine_source, "_configured_api_key", return_value="xai-key"),
            patch.object(
                imagine_source, "_cli_session_token", return_value="jwt"
            ) as mock_cli,
        ):
            self.assertEqual(imagine_source._get_bearer(), "xai-key")
            mock_cli.assert_not_called()


if __name__ == "__main__":
    unittest.main()
