import json
import unittest
from unittest.mock import MagicMock, patch

from app.services import kie_source
from app.models.schema import VideoAspect


def _response(payload, status=200):
    mock = MagicMock()
    mock.status_code = status
    mock.json.return_value = payload
    mock.raise_for_status.return_value = None
    return mock


def _submit_ok(task_id="kie-task-1"):
    return _response({"code": 200, "msg": "success", "data": {"taskId": task_id}})


def _poll_success(urls):
    return _response(
        {
            "code": 200,
            "data": {
                "state": "success",
                "resultJson": json.dumps({"resultUrls": urls}),
            },
        }
    )


def _poll_fail(msg="boom"):
    return _response({"code": 200, "data": {"state": "fail", "failMsg": msg}})


@patch.object(kie_source, "_get_api_key", return_value="test-key")
@patch.object(kie_source.material, "_get_tls_verify", return_value=True)
@patch.object(kie_source.utils, "task_dir", return_value="/tmp/kie-test-task")
@patch.object(kie_source, "_persist_tasks")
@patch.object(kie_source.time, "sleep")
class TestKieGenerateVideos(unittest.TestCase):
    def test_happy_path_submits_polls_downloads(
        self, mock_sleep, mock_persist, mock_dir, mock_tls, mock_key
    ):
        with (
            patch.object(
                kie_source.requests, "post", return_value=_submit_ok()
            ) as mock_post,
            patch.object(
                kie_source.requests,
                "get",
                return_value=_poll_success(["https://cdn/clip.mp4"]),
            ),
            patch.object(
                kie_source.material, "save_video", return_value="/tmp/clip.mp4"
            ) as mock_save,
        ):
            result = kie_source.generate_videos(
                "task-1", ["ocean waves"], VideoAspect.portrait, 10.0, 5
            )
        self.assertEqual(result, ["/tmp/clip.mp4"])
        payload = mock_post.call_args.kwargs["json"]
        self.assertEqual(payload["input"]["aspect_ratio"], "9:16")
        self.assertEqual(payload["input"]["duration"], 5)
        self.assertFalse(payload["input"]["generate_audio"])
        self.assertIn("ocean waves", payload["input"]["prompt"])
        mock_save.assert_called_once()
        # 提交后必须立即落盘 taskId（首次 persist 在任何轮询之前）。
        self.assertGreaterEqual(mock_persist.call_count, 1)
        first_records = mock_persist.call_args_list[0].args[1]
        self.assertEqual(first_records[0]["kie_task_id"], "kie-task-1")

    def test_max_clips_cap_respected(
        self, mock_sleep, mock_persist, mock_dir, mock_tls, mock_key
    ):
        terms = [f"term {i}" for i in range(10)]
        with (
            patch.object(
                kie_source.config.app,
                "get",
                side_effect=lambda k, d=None: {
                    "kie_max_clips": 2,
                    "kie_video_model": "",
                    "kie_poll_timeout": 600,
                }.get(k, d),
            ),
            patch.object(
                kie_source.requests, "post", return_value=_submit_ok()
            ) as mock_post,
            patch.object(kie_source.requests, "get", return_value=_poll_success(["u"])),
            patch.object(kie_source.material, "save_video", return_value="/tmp/x.mp4"),
        ):
            kie_source.generate_videos("task-1", terms, VideoAspect.portrait, 300.0, 5)
        self.assertEqual(mock_post.call_count, 2)

    def test_fail_state_returns_empty(
        self, mock_sleep, mock_persist, mock_dir, mock_tls, mock_key
    ):
        with (
            patch.object(kie_source.requests, "post", return_value=_submit_ok()),
            patch.object(kie_source.requests, "get", return_value=_poll_fail()),
        ):
            result = kie_source.generate_videos(
                "task-1", ["ocean"], VideoAspect.portrait, 10.0, 5
            )
        self.assertEqual(result, [])

    def test_submit_error_does_not_block_other_terms(
        self, mock_sleep, mock_persist, mock_dir, mock_tls, mock_key
    ):
        responses = [RuntimeError("http 500"), _submit_ok("kie-task-2")]

        def post_side_effect(*args, **kwargs):
            item = responses.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        with (
            patch.object(kie_source.requests, "post", side_effect=post_side_effect),
            patch.object(kie_source.requests, "get", return_value=_poll_success(["u"])),
            patch.object(kie_source.material, "save_video", return_value="/tmp/x.mp4"),
            patch.object(
                kie_source.config.app,
                "get",
                side_effect=lambda k, d=None: {
                    "kie_max_clips": 5,
                }.get(k, d),
            ),
        ):
            result = kie_source.generate_videos(
                "task-1", ["a", "b"], VideoAspect.portrait, 60.0, 5
            )
        self.assertEqual(result, ["/tmp/x.mp4"])

    def test_duration_clamped_to_seedance_range(
        self, mock_sleep, mock_persist, mock_dir, mock_tls, mock_key
    ):
        with (
            patch.object(
                kie_source.requests, "post", return_value=_submit_ok()
            ) as mock_post,
            patch.object(kie_source.requests, "get", return_value=_poll_success(["u"])),
            patch.object(kie_source.material, "save_video", return_value="/tmp/x.mp4"),
        ):
            kie_source.generate_videos("task-1", ["a"], VideoAspect.portrait, 5.0, 2)
        self.assertEqual(mock_post.call_args.kwargs["json"]["input"]["duration"], 4)


class TestKieResolution(unittest.TestCase):
    def _submit_with_resolution(self, configured):
        with (
            patch.object(kie_source, "_get_api_key", return_value="test-key"),
            patch.object(kie_source.material, "_get_tls_verify", return_value=True),
            patch.object(
                kie_source.utils, "task_dir", return_value="/tmp/kie-test-task"
            ),
            patch.object(kie_source, "_persist_tasks"),
            patch.object(kie_source.time, "sleep"),
            patch.object(
                kie_source.config.app,
                "get",
                side_effect=lambda k, d=None: {"kie_resolution": configured}.get(k, d),
            ),
            patch.object(
                kie_source.requests, "post", return_value=_submit_ok()
            ) as mock_post,
            patch.object(kie_source.requests, "get", return_value=_poll_success(["u"])),
            patch.object(kie_source.material, "save_video", return_value="/tmp/x.mp4"),
        ):
            kie_source.generate_videos(
                "task-1", ["ocean"], VideoAspect.portrait, 5.0, 5
            )
        return mock_post.call_args.kwargs["json"]["input"]["resolution"]

    def test_default_is_720p(self):
        self.assertEqual(self._submit_with_resolution(""), "720p")

    def test_480p_passthrough(self):
        self.assertEqual(self._submit_with_resolution("480p"), "480p")

    def test_invalid_value_falls_back_to_default(self):
        self.assertEqual(self._submit_with_resolution("1080p"), "720p")


class TestKieEnablement(unittest.TestCase):
    def test_missing_key_returns_empty(self):
        with patch.object(kie_source, "_get_api_key", return_value=""):
            result = kie_source.generate_videos("task-1", ["ocean"])
        self.assertEqual(result, [])

    def test_is_enabled_reflects_key(self):
        with patch.object(kie_source, "_get_api_key", return_value="k"):
            self.assertTrue(kie_source.is_enabled())
        with patch.object(kie_source, "_get_api_key", return_value=""):
            self.assertFalse(kie_source.is_enabled())


if __name__ == "__main__":
    unittest.main()
