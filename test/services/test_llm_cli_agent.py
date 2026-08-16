import subprocess
import unittest
from unittest.mock import patch

from app.services import llm_cli_agent
from app.services.llm_cli_agent import (
    _clean_cli_output,
    generate_via_cli,
)


def _completed(stdout="", stderr="", returncode=0):
    return subprocess.CompletedProcess(
        args=["x"], returncode=returncode, stdout=stdout, stderr=stderr
    )


class TestCleanCliOutput(unittest.TestCase):
    def test_strips_ansi_and_carriage_returns(self):
        raw = "\x1b[32mHello\x1b[0m world\r\n\x1b]0;title\x07done"
        self.assertEqual(_clean_cli_output(raw), "Hello world\ndone")

    def test_plain_text_untouched(self):
        self.assertEqual(_clean_cli_output("  plain text  "), "plain text")

    def test_strips_kimi_bullet_prefixes(self):
        self.assertEqual(_clean_cli_output("• OK\n• line two"), "OK\nline two")


@patch.object(llm_cli_agent.config, "is_running_in_container", return_value=False)
@patch.object(llm_cli_agent, "_resolve_binary", side_effect=lambda b: f"/bin/{b}")
class TestGenerateViaCli(unittest.TestCase):
    def test_claude_uses_stdin(self, mock_resolve, mock_container):
        with patch.object(subprocess, "run", return_value=_completed("OK")) as run:
            result = generate_via_cli("prompt text", {"command": "claude"})
        self.assertEqual(result, "OK")
        argv = run.call_args.args[0]
        self.assertEqual(argv, ["/bin/claude", "-p", "--output-format", "text"])
        self.assertEqual(run.call_args.kwargs["input"], "prompt text")
        self.assertIsNotNone(run.call_args.kwargs["timeout"])

    def test_grok_and_kimi_pass_prompt_as_argv(self, mock_resolve, mock_container):
        for backend, fmt in (("grok", "plain"), ("kimi", "text")):
            with patch.object(subprocess, "run", return_value=_completed("OK")) as run:
                generate_via_cli("prompt text", {"command": backend})
            argv = run.call_args.args[0]
            self.assertEqual(
                argv,
                [f"/bin/{backend}", "-p", "prompt text", "--output-format", fmt],
            )
            self.assertIsNone(run.call_args.kwargs["input"])

    def test_fallback_chain_order(self, mock_resolve, mock_container):
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(argv[0])
            if "claude" in argv[0]:
                raise subprocess.TimeoutExpired(cmd=argv, timeout=1)
            return _completed("fallback answer")

        with patch.object(subprocess, "run", side_effect=fake_run):
            result = generate_via_cli(
                "p", {"command": "claude", "fallback_backends": "grok,kimi"}
            )
        self.assertEqual(result, "fallback answer")
        self.assertEqual(calls, ["/bin/claude", "/bin/grok"])

    def test_all_backends_failed_raises(self, mock_resolve, mock_container):
        with patch.object(
            subprocess, "run", return_value=_completed("", "boom", returncode=1)
        ):
            with self.assertRaises(RuntimeError) as ctx:
                generate_via_cli("p", {"command": "claude"})
        self.assertIn("all cli backends failed", str(ctx.exception))

    def test_unknown_backend_rejected(self, mock_resolve, mock_container):
        with self.assertRaises(RuntimeError):
            generate_via_cli("p", {"command": "gpt4all"})

    def test_timeout_config_parsed(self, mock_resolve, mock_container):
        with patch.object(subprocess, "run", return_value=_completed("OK")) as run:
            generate_via_cli("p", {"command": "claude", "timeout_seconds": "120"})
        self.assertEqual(run.call_args.kwargs["timeout"], 120)
        with patch.object(subprocess, "run", return_value=_completed("OK")) as run:
            generate_via_cli("p", {"command": "claude", "timeout_seconds": "junk"})
        self.assertEqual(run.call_args.kwargs["timeout"], 90)

    def test_error_string_via_generate_response(self, mock_resolve, mock_container):
        from app.services import llm

        app_config = {"llm_provider": "cli_agent", "cli_agent_command": "claude"}
        with patch.object(
            subprocess, "run", side_effect=subprocess.TimeoutExpired(cmd="c", timeout=1)
        ):
            result = llm._generate_response("p", app_config=app_config)
        self.assertTrue(result.startswith("Error: "))


class TestContainerGate(unittest.TestCase):
    def test_rejected_inside_container(self):
        with patch.object(
            llm_cli_agent.config, "is_running_in_container", return_value=True
        ):
            with self.assertRaises(RuntimeError) as ctx:
                generate_via_cli("p", {"command": "claude"})
        self.assertIn("Docker", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
