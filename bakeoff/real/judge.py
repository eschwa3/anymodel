"""Optional LLM-judge re-grading for the free-text tasks (R1, R2, R3, R7).

Never called unless `--judge-model` is passed. Judge scores are stored
alongside the heuristic score (`scoring.py`), never replacing it -- see
`bakeoff/README.md`. Implemented by shelling out to the existing
`anymodel-worker` CLI in read-only mode against an empty throwaway git repo
(no new API client code), the same way the rest of the harness invokes
workers.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

_SCORE_RE = re.compile(r'"score"\s*:\s*([01](?:\.\d+)?)')
_FALLBACK_SCORE_RE = re.compile(r"\bscore\s*[:=]\s*([01](?:\.\d+)?)")

_JUDGE_ROLE_PROMPT = (
    "You are a strict grading assistant. You will be given a task's original "
    "prompt, an answer key (ground truth), and a worker's answer. Score the "
    "worker's answer for factual correctness and completeness against the "
    "answer key on a 0.0-1.0 scale (1.0 = fully correct and complete, 0.0 = "
    "wrong or fabricated). Reply with ONLY a single-line JSON object: "
    '{"score": <float 0-1>, "reason": "<one sentence>"}. No other text.'
)


def _empty_git_repo(tmp_dir: Path) -> Path:
    repo = tmp_dir / "empty_repo"
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "README.md").write_text("empty repo for LLM-judge grading; not a real project.\n")
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "bakeoff-judge",
        "GIT_AUTHOR_EMAIL": "judge@example.invalid",
        "GIT_COMMITTER_NAME": "bakeoff-judge",
        "GIT_COMMITTER_EMAIL": "judge@example.invalid",
    }
    subprocess.run(
        ["git", "init", "-q"], cwd=repo, env=env, capture_output=True, timeout=30, check=False
    )
    subprocess.run(
        ["git", "add", "-A"], cwd=repo, env=env, capture_output=True, timeout=30, check=False
    )
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "commit", "-q", "-m", "init"],
        cwd=repo,
        env=env,
        capture_output=True,
        timeout=30,
        check=False,
    )
    return repo


def build_judge_prompt(task_prompt: str, answer_key: str, worker_answer: str) -> str:
    return (
        f"## Original task prompt\n{task_prompt.strip()}\n\n"
        f"## Answer key (ground truth -- not shown to the worker)\n{answer_key.strip()}\n\n"
        f"## Worker's answer\n{worker_answer.strip()}\n\n"
        "Score the worker's answer as instructed."
    )


def parse_judge_score(final_message: str) -> dict[str, Any]:
    text = final_message or ""
    match = _SCORE_RE.search(text) or _FALLBACK_SCORE_RE.search(text)
    if match is None:
        return {"score": None, "raw": text, "parse_error": "no score found in judge output"}
    score = max(0.0, min(1.0, float(match.group(1))))
    reason_match = re.search(r'"reason"\s*:\s*"([^"]*)"', text)
    return {"score": score, "reason": reason_match.group(1) if reason_match else None, "raw": text}


async def judge_free_text(
    task_prompt: str,
    answer_key: str,
    worker_answer: str,
    judge_model: str,
    timeout: float = 120.0,
) -> dict[str, Any]:
    """Re-grade one free-text answer with `judge_model`, via the CLI in
    read-only mode against an empty temp repo. Returns
    `{"score": float|None, "reason": str|None, "raw": str, ...}`.
    """
    with tempfile.TemporaryDirectory(prefix="bakeoff-judge-") as tmp:
        repo = _empty_git_repo(Path(tmp))
        judge_prompt = build_judge_prompt(task_prompt, answer_key, worker_answer)
        cmd = [
            "uv",
            "run",
            "anymodel-worker",
            "run",
            "--model",
            judge_model,
            "--cwd",
            str(repo),
            "--prompt",
            judge_prompt,
            "--mode",
            "read-only",
            "--role-prompt",
            _JUDGE_ROLE_PROMPT,
            "--max-turns",
            "3",
            "--timeout",
            str(timeout),
            "--json",
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=os.environ.copy(),
            )
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout + 30)
        except (OSError, TimeoutError) as exc:
            return {"score": None, "raw": None, "parse_error": f"judge invocation failed: {exc}"}

        try:
            cli_json = json.loads(stdout_b.decode("utf-8", "replace").strip())
        except (json.JSONDecodeError, ValueError):
            return {
                "score": None,
                "raw": stdout_b.decode("utf-8", "replace"),
                "parse_error": "judge CLI did not print valid JSON",
                "stderr_tail": stderr_b.decode("utf-8", "replace")[-1000:],
            }

    return parse_judge_score(cli_json.get("final_message", ""))
