"""
Codex CLI subprocess client.

Shells out to the `codex` CLI (OpenAI's Codex) in non-interactive mode:
    codex exec --model <model> <prompt>

Authentication comes from `codex login` (ChatGPT Plus/Pro/Team subscription),
not OPENAI_API_KEY.

NOTE: Codex CLI must be installed separately from Codex Desktop — e.g.
    npm install -g @openai/codex
If the `codex` binary is not on PATH this client raises a helpful error.
"""

from __future__ import annotations

import shutil
import subprocess

from llm_clients.base import RunResult
from llm_clients.json_output import build_json_prompt, parse_output


_CLI = "codex"


class CodexClient:
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
                "Codex CLI not found on PATH. Install with "
                "`npm install -g @openai/codex` (or the Homebrew/binary "
                "equivalent), then run `codex login` to authenticate "
                "with your ChatGPT subscription."
            )
        self._model = model
        self._output_type = output_type
        self._system_prompt = system_prompt
        self._max_retries = max_retries
        self._timeout = timeout_seconds

    def run_sync(self, prompt: str) -> RunResult:
        full_prompt = self._compose_prompt(prompt)
        raw = self._invoke(full_prompt)

        def _retry(feedback: str) -> str:
            return self._invoke(f"{full_prompt}\n\n---\n{feedback}")

        parsed = parse_output(
            raw,
            self._output_type,
            retry=_retry,
            max_retries=self._max_retries,
        )
        return RunResult(output=parsed)

    # ------------------------------------------------------------------

    def _compose_prompt(self, user_prompt: str) -> str:
        # Codex CLI does not expose a separate system prompt flag, so prepend
        # it to the user message inside a clear delimiter.
        schema_prompt = build_json_prompt(user_prompt, self._output_type)
        return (
            f"[SYSTEM INSTRUCTIONS]\n{self._system_prompt}\n"
            f"[END SYSTEM INSTRUCTIONS]\n\n"
            f"{schema_prompt}"
        )

    def _invoke(self, prompt: str) -> str:
        args = [_CLI, "exec", "--model", self._model, prompt]
        try:
            proc = subprocess.run(
                args,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=self._timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"Codex CLI timed out after {self._timeout}s") from exc

        if proc.returncode != 0:
            raise RuntimeError(
                f"Codex CLI exited {proc.returncode}.\n"
                f"stderr: {proc.stderr.strip()[:2000]}"
            )

        return proc.stdout.strip()
