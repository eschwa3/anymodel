"""`anymodel-worker` CLI: run a single worker outside the MCP layer."""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import os
import sys
from pathlib import Path

from anymodel_subagents.engine import build_system_prompt, run_worker
from anymodel_subagents.openrouter import OpenRouterClient
from anymodel_subagents.types import Mode

DEFAULT_ROLE_PROMPT = "You are a helpful, careful software engineering assistant."


def _log(msg: str) -> None:
    print(msg, file=sys.stderr)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="anymodel-worker")
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="Run a single worker to completion")
    run_p.add_argument("--model", required=True, help="OpenRouter model id")
    run_p.add_argument("--cwd", required=True, type=Path, help="Workspace root for the worker")
    prompt_group = run_p.add_mutually_exclusive_group(required=True)
    prompt_group.add_argument("--prompt", help="Task prompt text")
    prompt_group.add_argument(
        "--prompt-file", type=Path, help="Path to a file with the task prompt"
    )
    run_p.add_argument("--mode", choices=["read-only", "edit", "edit+bash"], default="read-only")
    run_p.add_argument("--role-prompt", default=DEFAULT_ROLE_PROMPT)
    run_p.add_argument("--max-turns", type=int, default=40)
    run_p.add_argument("--timeout", type=float, default=900.0)
    run_p.add_argument("--transcript", type=Path, default=None)
    run_p.add_argument("--json", action="store_true", dest="json_output")

    return parser


async def _run_command(args: argparse.Namespace) -> int:
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        _log("Error: the OPENROUTER_API_KEY environment variable is not set.")
        return 2

    if args.prompt is not None:
        task_prompt = args.prompt
    else:
        try:
            task_prompt = args.prompt_file.read_text()
        except OSError as exc:
            _log(f"Error: could not read --prompt-file: {exc}")
            return 2

    # Imported lazily: tools/ is owned by another workstream and may not be import-safe yet.
    from anymodel_subagents.config import load_config, state_dir, validate_cwd
    from anymodel_subagents.tools import LocalWorkspace, tools_for_mode
    from anymodel_subagents.tools.bash import BashPolicy

    cfg = load_config()
    # The same cwd policy the MCP `dispatch` path enforces (absolute, exists,
    # not the filesystem root or the user's home directory or an ancestor of
    # it, inside a git work tree, inside `allowed_roots` if configured, and
    # not inside the plugin's own state dir) -- a bare `is_dir()` check would
    # let this CLI point a worker at paths `dispatch` would refuse outright.
    try:
        cwd = validate_cwd(str(args.cwd.resolve()), cfg)
    except ValueError as exc:
        _log(f"Error: --cwd is invalid: {exc}")
        return 2

    mode: Mode = args.mode
    ws = LocalWorkspace(cwd)
    tools = tools_for_mode(
        mode,
        bash_policy=BashPolicy(
            allow_prefixes=cfg.bash_allow,
            allow_unsandboxed=cfg.allow_unsandboxed_bash,
            state_dir=state_dir(),
        ),
    )
    system_prompt = build_system_prompt(args.role_prompt, ws, mode)

    client = OpenRouterClient(
        api_key,
        max_output_tokens=cfg.max_output_tokens,
        provider_sort=cfg.provider_sort,
    )
    try:
        result = await run_worker(
            client=client,
            model=args.model,
            system_prompt=system_prompt,
            task_prompt=task_prompt,
            tools=tools,
            ws=ws,
            max_turns=args.max_turns,
            timeout_s=args.timeout,
            transcript_path=args.transcript,
        )
    finally:
        await client.aclose()

    if args.json_output:
        print(json.dumps(dataclasses.asdict(result), default=str))
    else:
        _log(f"status: {result.status}")
        _log(
            f"turns: {result.turns}  tool_calls: {result.tool_calls}  "
            f"invalid_tool_calls: {result.invalid_tool_calls}"
        )
        _log(f"usage: {result.usage}")
        _log(f"changed_files: {result.changed_files}")
        print(result.final_message)

    return 0 if result.status == "completed" else 1


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    if args.command == "run":
        sys.exit(asyncio.run(_run_command(args)))
    parser.print_help(sys.stderr)
    sys.exit(2)


if __name__ == "__main__":
    main()
