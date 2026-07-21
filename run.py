"""
run.py — sequencer for the local half of the pipeline.

Two behaviours, chosen by whether ANTHROPIC_API_KEY is set:

  API mode (key set): runs the whole local half stop-on-error:
      scraper -> generator -> editor -> imagegen -> final_check

  Claude Code mode (DEFAULT, no key): generation is interactive (this Claude
  Code session is the writer), so the run can't fully autopilot the middle. It
  runs   scraper -> generator --prep   and then STOPS with a hand-off checklist.
  After Claude Code writes the article and you run `python generator.py --ingest`
  + `python editor.py`, finish the media/gate stages with:
      python run.py --finish        # imagegen -> final_check

Publishing (publisher.py) is always a separate, human-gated step.

Usage:
    python run.py                 # full local pipeline (API) or scrape+prep (Claude Code)
    python run.py --skip-scraper  # start at generator (you wrote keyword.json)
    python run.py --finish        # run imagegen -> final_check (after post.json is ready)
"""

import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent


def _run(script: str, label: str, idx: int, total: int, extra: list[str] | None = None) -> int:
    print(f"\n{'=' * 66}")
    print(f"[run] Stage {idx}/{total}: {label}  ({script})")
    print("=" * 66)
    return subprocess.run([sys.executable, str(HERE / script), *(extra or [])]).returncode


def main() -> int:
    args = sys.argv[1:]
    has_key = bool(os.environ.get("ANTHROPIC_API_KEY"))

    if "--finish" in args:
        stages = [("seo_check.py", "SEO / AEO scorecard"),
                  ("imagegen.py", "Generate + QA images"),
                  ("final_check.py", "Pre-publish check")]
        for i, (s, lbl) in enumerate(stages, 1):
            rc = _run(s, lbl, i, len(stages))
            if rc != 0:
                print(f"\n[run] {s} exited {rc}. Stopping.")
                return rc
        print("\n[run] Media + gate complete. Next: python publisher.py --check / --publish.")
        return 0

    if has_key:
        stages = [
            ("scraper.py", "Pick a keyword"),
            ("generator.py", "Write the article (Claude API)"),
            ("editor.py", "Review / quality gate"),
            ("seo_check.py", "SEO / AEO scorecard"),
            ("imagegen.py", "Generate + QA images"),
            ("final_check.py", "Pre-publish check"),
        ]
        if "--skip-scraper" in args:
            stages = [s for s in stages if s[0] != "scraper.py"]
        for i, (s, lbl) in enumerate(stages, 1):
            rc = _run(s, lbl, i, len(stages))
            if rc != 0:
                print(f"\n[run] {s} exited {rc}. Stopping. Fix the issue above, then "
                      f"re-run (add --skip-scraper to keep the current keyword).")
                return rc
        print(f"\n{'=' * 66}")
        print("[run] Local pipeline complete. Next: python publisher.py --check / --publish.")
        print("=" * 66)
        return 0

    # ── Claude Code mode ──
    stages = [("scraper.py", "Pick a keyword"),
              ("generator.py", "Prep the generation request", ["--prep"])]
    if "--skip-scraper" in args:
        stages = [s for s in stages if s[0] != "scraper.py"]
    for i, stage in enumerate(stages, 1):
        script, lbl = stage[0], stage[1]
        extra = stage[2] if len(stage) > 2 else None
        rc = _run(script, lbl, i, len(stages), extra)
        if rc != 0:
            print(f"\n[run] {script} exited {rc}. Stopping.")
            return rc
    print(f"\n{'=' * 66}")
    print("[run] Claude Code mode — generation is interactive. Hand-off checklist:")
    print("  1. Ask Claude Code to read generation_request.json and write the")
    print("     article JSON to generation_response.json.")
    print("  2. python generator.py --ingest     (validate -> post.json)")
    print("  3. python editor.py                 (code checks + Layer-2 review brief)")
    print("  4. python run.py --finish           (SEO/AEO scorecard -> imagegen -> final_check)")
    print("  5. python publisher.py --check / --publish")
    print("=" * 66)
    return 0


if __name__ == "__main__":
    sys.exit(main())
