"""
Claude Code subprocess client.

Shells out to the `claude` CLI (installed with Claude Code) in headless mode:
    claude -p <prompt> --model <model> --output-format json

The prompt is piped via stdin to avoid argv length limits. The system prompt
is passed via --append-system-prompt so PydanticAI-style behaviour is preserved.
Authentication comes from your Claude Code login (no ANTHROPIC_API_KEY spent).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from typing import Any

from llm_clients.base import RunResult
from llm_clients.json_output import build_json_prompt, parse_output


_CLI = "claude"


class ClaudeCodeClient:
    def __init__(
        self,
        model: str,
        output_type: type,
        system_prompt: str,
        max_retries: int = 2,
        timeout_seconds: int = 600,
    ) -> None:
        if shutil.which(_CLI) is None:
            raise RuntimeError(
                "Claude CLI not found on PATH. Install Claude Code "
                "(https://claude.com/product/claude-code) and re-run."
            )
        self._model = model
        self._output_type = output_type
        self._system_prompt = system_prompt
        self._max_retries = max_retries
        self._timeout = timeout_seconds

    def run_sync(self, prompt: str) -> RunResult:
        full_prompt = build_json_prompt(prompt, self._output_type)
        raw = self._invoke(full_prompt)

        def _retry(feedback: str) -> str:
            # Stateless retry: repeat the original task + validation feedback.
            return self._invoke(f"{full_prompt}\n\n---\n{feedback}")

        parsed = parse_output(
            raw,
            self._output_type,
            retry=_retry,
            max_retries=self._max_retries,
        )
        return RunResult(output=parsed)

    # ------------------------------------------------------------------

    def _invoke(self, prompt: str) -> str:
        args = [
            _CLI,
            "-p",
            "--model", self._model,
            "--output-format", "json",
            "--append-system-prompt", self._system_prompt,
        ]
        try:
            proc = subprocess.run(
                args,
                input=prompt,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=self._timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"Claude CLI timed out after {self._timeout}s") from exc

        if proc.returncode != 0:
            raise RuntimeError(
                f"Claude CLI exited {proc.returncode}.\n"
                f"stderr: {proc.stderr.strip()[:2000]}"
            )

        return _extract_result_text(proc.stdout)


def _extract_result_text(stdout: str) -> str:
    """
    Pull the assistant's final text out of claude --output-format json.

    The JSON envelope looks like:
        {"type":"result","subtype":"success","result":"<text>", ...}
    Falls back to raw stdout if parsing fails so callers see something.
    """
    stdout = stdout.strip()
    if not stdout:
        return ""
    try:
        payload: Any = json.loads(stdout)
    except json.JSONDecodeError:
        return stdout

    if isinstance(payload, dict) and "result" in payload:
        return str(payload["result"])
    return stdout
