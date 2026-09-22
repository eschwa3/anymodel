"""Replay of a real anymodel-subagents run, for the README demo GIF.

Every job id, model, duration, cost and report line below is copied from the
ledger and job records of swarm s-1b02e8e5 (2026-09-22). Only the waiting is
compressed. Render with: vhs docs/demo/demo.tape
"""

import sys
import time

DIM, BOLD, CYAN, GREEN, YELLOW, RESET = (
    "\033[2m",
    "\033[1m",
    "\033[36m",
    "\033[32m",
    "\033[33m",
    "\033[0m",
)


def out(text: str = "", pause: float = 0.0) -> None:
    print(text, flush=True)
    time.sleep(pause)


def typed(text: str, delay: float = 0.018) -> None:
    for ch in text:
        sys.stdout.write(ch)
        sys.stdout.flush()
        time.sleep(delay)
    print()


def main() -> None:
    sys.stdout.write(f"{BOLD}> {RESET}")
    typed(
        "Use anymodel workers: have a reviewer check ledger.py and a researcher\n"
        "  list every place we read OPENROUTER_API_KEY. Report the cost."
    )
    time.sleep(0.8)
    out()
    out(f"{CYAN}● dispatch{RESET}  2 jobs · swarm s-1b02e8e5", 0.3)
    out(f"  {DIM}j-36fb3fdc  researcher  deepseek/deepseek-v4.1-flash  read-only{RESET}", 0.2)
    out(f"  {DIM}j-c6a1ea53  reviewer    deepseek/deepseek-v4.1-flash  read-only{RESET}", 0.6)
    out(f"{CYAN}● wait{RESET}      {DIM}the orchestrator idles; workers read the repo{RESET}", 0.2)
    for label in ("0:30", "1:15  researcher done", "4:00", "8:14  reviewer done"):
        out(f"  {DIM}… {label}{RESET}", 0.45)
    out()
    out(f"{CYAN}● results{RESET}", 0.3)
    out(f"  {GREEN}✓{RESET} researcher  1m15s   5 turns   {YELLOW}$0.0028{RESET}", 0.2)
    out(f"  {GREEN}✓{RESET} reviewer    8m14s  12 turns   {YELLOW}$0.0142{RESET}", 0.8)
    out()
    out(f"{BOLD}Researcher{RESET} — 3 runtime reads in src/, each with file:line:", 0.2)
    out('  server.py:934  api_key = os.environ.get("OPENROUTER_API_KEY")', 0.15)
    out("  server.py:967  used only as a redaction seed", 0.15)
    out("  cli.py:47      anymodel-worker run", 0.8)
    out(f"{BOLD}Reviewer{RESET} — 4 findings in ledger.py, each with a repro:", 0.2)
    out(f"  {YELLOW}medium{RESET}  summarize() crashes on a non-object ledger line", 0.15)
    out(f"  {DIM}low{RESET}     spent_on() can raise UnicodeDecodeError", 0.15)
    out(f"  {DIM}low{RESET}     day groups use the UTC date, the budget uses local", 0.15)
    out(f"  {DIM}low{RESET}     an existing ledger's permissions are not tightened", 0.9)
    out()
    out(
        f"{BOLD}Cost: {YELLOW}$0.017{RESET}{BOLD} of OpenRouter{RESET} for 17 turns · 334k tokens read by workers",
        3.0,
    )


if __name__ == "__main__":
    main()
