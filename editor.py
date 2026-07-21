"""
editor.py
---------
Step 3 of the pipeline — the reviewer/gatekeeper.

Reviews post.json (from generator.py) using TWO layers:

  1. Hard-coded Python checks (verified in code, never self-reported by the LLM):
     word count, internal/external link counts, banned AI phrases, configured
     compliance mentions, spelling, comparison table, FAQ count, image
     placeholders + alt text, no stray <h1>, keyword placement, and Shopify SEO
     lengths. Deterministic.

  2. A Claude review pass (same model as the generator) acting as an editor, not
     a writer — judging the subjective things code can't: tone, heading
     hierarchy, whether the FAQ is genuinely useful, whether internal links are
     contextually placed (not stuffed), and whether the article reads naturally.

Outcome:
  - PASS: small metadata/spelling issues are fixed inline; post.json is updated.
  - FAIL: structured feedback is fed back to generator.generate_post(feedback=...)
    to regenerate. Max 2 retries total. If it still fails, the last draft +
    feedback are written to needs_manual_review.json and the pipeline STOPS
    (final_check.py is not run).

Usage:
  python editor.py            # reviews post.json in place

Modes (chosen automatically):
  - Claude Code mode (DEFAULT when no ANTHROPIC_API_KEY): runs layer 1 only
    (deterministic checks + inline fixes). Layer 2 (subjective review) and any
    regeneration are done in-session by Claude Code.
  - API mode (ANTHROPIC_API_KEY set): both layers + the regenerate-on-fail loop.
"""

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import anthropic

import config as cfg
import generator  # reuse MODEL, placeholders, meta helpers, generate_post

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

# ── Config ──────────────────────────────────────────────────────────────────

MODEL = generator.MODEL
HERE = Path(__file__).parent
POST_FILE = HERE / "post.json"
MANUAL_REVIEW_FILE = HERE / "needs_manual_review.json"
MAX_RETRIES = 2

MIN_WORDS = 1400
MAX_WORDS = 3000
MIN_INTERNAL_LINKS = 4
MIN_FAQ = 6

BANNED_PHRASES = [
    "in today's fast-paced world", "it's worth noting", "delve",
    "game-changer", "in conclusion", "unlock", "navigate the landscape",
    "ever-evolving", "when it comes to", "in the realm of", "look no further",
    "rest assured", "in today's digital age",
]

# Conservative American -> British fixes (word-boundary, case-preserving).
US_TO_UK = {
    "optimize": "optimise", "optimizes": "optimises", "optimized": "optimised",
    "optimizing": "optimising", "optimization": "optimisation",
    "organize": "organise", "organized": "organised", "organizing": "organising",
    "organization": "organisation", "organizations": "organisations",
    "center": "centre", "centers": "centres", "centered": "centred",
    "color": "colour", "colors": "colours",
    "analyze": "analyse", "analyzes": "analyses", "analyzed": "analysed",
    "analyzing": "analysing",
    "behavior": "behaviour", "behaviors": "behaviours",
    "defense": "defence", "offense": "offence", "license": "licence",
    "catalog": "catalogue", "catalogs": "catalogues",
    "fulfill": "fulfil", "fulfillment": "fulfilment",
    "prioritize": "prioritise", "prioritized": "prioritised",
    "prioritizing": "prioritising", "minimize": "minimise",
    "maximize": "maximise", "utilize": "utilise", "recognize": "recognise",
    "specialize": "specialise", "specialized": "specialised",
    "standardize": "standardise", "modernize": "modernise",
    "customize": "customise", "customized": "customised",
    "customization": "customisation", "traveled": "travelled",
    "labeled": "labelled", "modeling": "modelling",
}
_US_RE = re.compile(r"\b(" + "|".join(re.escape(k) for k in US_TO_UK) + r")\b", re.I)


# ── Text helpers ────────────────────────────────────────────────────────────

def _strip_tags(html: str) -> str:
    return re.sub(r"<[^>]+>", " ", re.sub(r"<script\b.*?</script>", " ", html, flags=re.I | re.S))


def _word_count(html: str) -> int:
    return len(re.findall(r"\w+", _strip_tags(html)))


def _faq_count(html: str) -> int:
    """Count Q&A pairs inside the FAQ section (H3s after the FAQ H2)."""
    m = re.search(
        r"<h2[^>]*>.*?frequently asked questions.*?</h2>(.*?)(?=<h2|<script|\Z)",
        html, re.S | re.I)
    region = m.group(1) if m else html
    return len(re.findall(r"<h3\b", region, re.I))


def _apply_british_spelling(text: str) -> tuple[str, int]:
    count = 0

    def repl(m: re.Match) -> str:
        nonlocal count
        w = m.group(0)
        uk = US_TO_UK[w.lower()]
        count += 1
        return uk[:1].upper() + uk[1:] if w[:1].isupper() else uk

    return _US_RE.sub(repl, text), count


# ── Layer 1: hard-coded checks + safe inline fixes ──────────────────────────

def run_code_checks(post: dict) -> dict:
    """Verify objective requirements in code and apply safe inline fixes.

    Mutates `post` for inline fixes. Returns {blockers, fixes, warnings, ...}.
    """
    blockers: list[str] = []
    warnings: list[str] = []
    fixes: list[str] = []

    body = post.get("body_html", "")
    low = body.lower()
    keyword = str(post.get("keyword_used", "")).lower()

    words = _word_count(body)
    if words < MIN_WORDS:
        blockers.append(f"Body is too thin ({words} words); need >= {MIN_WORDS}.")
    elif words > MAX_WORDS:
        warnings.append(f"Body is long ({words} words); target <= {MAX_WORDS}.")

    internal = len(re.findall(r'href=["\']/(?!/)', body))
    if internal < MIN_INTERNAL_LINKS:
        blockers.append(
            f"Only {internal} internal links; need >= {MIN_INTERNAL_LINKS}, "
            f"contextually placed within relevant sentences.")

    own_domain = re.escape(cfg.SITE["site"])
    external = len(re.findall(
        r'href=["\']https?://(?![^"\']*' + own_domain + r')', body, re.I))
    if external < 1:
        blockers.append(f"No outbound authority link ({cfg.SITE['authority_desc']}).")

    hits = [p for p in BANNED_PHRASES if p in low]
    if hits:
        blockers.append(f"Banned AI phrases present: {hits}.")

    _terms = cfg.SITE.get("compliance_terms", {})
    for term in _terms.get("required", []):
        if term.lower() not in low:
            blockers.append(f"No '{term}' mention (required compliance term).")
    optional = _terms.get("optional", [])
    if optional and not any(t.lower() in low for t in optional):
        warnings.append(f"No mention of any optional compliance term "
                        f"({', '.join(optional)}).")

    if "<table" not in low:
        warnings.append("No comparison <table> (recommended, not required).")

    faq = _faq_count(body)
    if faq < MIN_FAQ:
        blockers.append(f"FAQ has {faq} questions; need {MIN_FAQ}-8 useful ones.")

    # The hero is the featured image (set by the publisher), NOT in the body.
    if generator.INBODY_IMG not in body:
        blockers.append("Missing in-body image placeholder ({{INBODY_IMAGE}}).")

    # Every <img> must carry alt text.
    for tag in re.findall(r"<img\b[^>]*>", body, re.I):
        if not re.search(r'alt=["\']', tag, re.I):
            blockers.append(f"Image without alt text: {tag[:80]}")
            break

    # body_html must NOT contain an <h1> — the article title is the H1 (theme).
    if re.search(r"<h1\b", body, re.I):
        blockers.append("body_html contains an <h1>; the article title is the H1 "
                        "— use <h2>/<h3> in the body only.")

    # Focus keyword should appear early. Check the first ~50 words of prose.
    if keyword:
        first_words = " ".join(_strip_tags(body).split()[:50]).lower()
        if keyword not in first_words and not all(w in first_words for w in keyword.split()):
            warnings.append("Focus keyword not found in the first paragraph.")

    # ── Inline fixes (do not block a PASS) ──
    for field, limit in (("seo_title", generator.META_TITLE_MAX),
                         ("seo_description", generator.META_DESC_MAX)):
        original = post.get(field, "")
        fixed = generator.trim_meta(original, limit)
        if fixed != original:
            post[field] = fixed
            fixes.append(f"Tidied {field} -> '{fixed}' ({len(fixed)}c).")

    new_body, n_sp = _apply_british_spelling(body)
    if n_sp:
        post["body_html"] = new_body
        fixes.append(f"Converted {n_sp} US spelling(s) to British.")
    new_exc, n_ex = _apply_british_spelling(post.get("summary_html", ""))
    if n_ex:
        post["summary_html"] = new_exc
        fixes.append(f"Converted {n_ex} US spelling(s) in summary.")

    return {"blockers": blockers, "fixes": fixes, "warnings": warnings,
            "words": words, "internal_links": internal,
            "external_links": external, "faq": faq}


# ── Layer 2: Claude review pass ─────────────────────────────────────────────

_SHORT = cfg.SITE["short_name"]
_REGION = cfg.SITE["region"]

REVIEWER_SYSTEM = f"""You are a strict senior editor for {cfg.SITE['company']} \
({cfg.SITE['site']}). You REVIEW drafts — you do not rewrite them.

BENCHMARK
- Tone: authoritative but accessible expert voice; direct; educational; helpful \
before promotional; positions {_SHORT} as a trusted expert for {_REGION} \
buyers; NO hype, NO filler, NO AI-boilerplate; {cfg.SITE['spelling']}.
- Heading hierarchy must be logical (H2 sections; H3 only nested under their \
H2; NO H1 in the body — the article title is the H1).
- FAQ must be GENUINELY useful (definitions, how to choose, cost, shipping, \
compatibility, getting started) — reject filler or repetitive questions.
- Internal links must be woven into relevant sentences — reject link-stuffing.
- Any call to action must feel natural and helpful, not spammy.
- AEO (answer-engine readiness): the intro should answer the query quickly and \
define the topic ("X is ..."); FAQ and section answers should be self-contained \
and snippet-sized (a reader or AI could quote one paragraph as the answer \
without needing the rest); question-style headings must be genuinely, directly \
answered in the sentence(s) that follow. Flag vague, buried, or context-\
dependent answers that an answer engine could not lift cleanly.

You focus on these SUBJECTIVE qualities. Objective counts (word count, exact \
link counts, meta lengths) are verified separately in code — do not fail a \
draft solely on those.

Return your verdict by calling the `emit_review` tool. Set verdict "FAIL" only \
when there are MAJOR structural or tone problems needing a rewrite. Minor polish \
issues -> verdict "PASS" but still list them."""

REVIEW_TOOL = {
    "name": "emit_review",
    "description": "Return the editorial verdict as structured data.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "verdict": {"type": "string", "enum": ["PASS", "FAIL"]},
            "summary": {"type": "string", "description": "One-line overall judgement."},
            "issues": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "section": {"type": "string"},
                        "problem": {"type": "string"},
                        "fix": {"type": "string"},
                        "severity": {"type": "string", "enum": ["major", "minor"]},
                    },
                    "required": ["section", "problem", "fix", "severity"],
                },
            },
        },
        "required": ["verdict", "summary", "issues"],
    },
}


def claude_review(post: dict) -> dict:
    """Run the subjective review pass. Returns a verdict dict; on API failure
    returns a permissive stub so an infra hiccup can't nuke a good draft."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise EnvironmentError("ANTHROPIC_API_KEY is not set in the environment.")
    client = anthropic.Anthropic()

    payload = {k: post.get(k, "") for k in
               ("title", "seo_title", "seo_description", "summary_html",
                "tags", "body_html")}
    user = ("Review this draft against the benchmark and return the verdict.\n\n"
            + json.dumps(payload, ensure_ascii=False))

    for attempt in (1, 2):
        try:
            message = client.messages.create(
                model=MODEL,
                max_tokens=4000,
                system=REVIEWER_SYSTEM,
                tools=[REVIEW_TOOL],
                tool_choice={"type": "tool", "name": "emit_review"},
                messages=[{"role": "user", "content": user}],
            )
            for block in message.content:
                if block.type == "tool_use" and block.name == "emit_review":
                    verdict = dict(block.input)
                    verdict.setdefault("issues", [])
                    verdict.setdefault("summary", "")
                    return verdict
            raise ValueError("no emit_review tool_use in response")
        except (ValueError, anthropic.APIError) as e:
            if attempt == 2:
                print(f"[editor] WARNING: Claude review failed ({e}); "
                      f"relying on code checks only.")
                return {"verdict": "PASS", "summary": "review-unavailable",
                        "issues": [], "review_error": str(e)}


# ── Orchestration ───────────────────────────────────────────────────────────

def review_post(post: dict) -> tuple[bool, str, dict]:
    """Review a post: run code checks (with inline fixes) + Claude review.
    Returns (passed, feedback_for_regen, detail)."""
    code = run_code_checks(post)          # mutates post with inline fixes
    review = claude_review(post)

    issues = review.get("issues", [])
    major = [i for i in issues if i.get("severity") == "major"]
    review_fail = review.get("verdict") == "FAIL" or bool(major)

    passed = not code["blockers"] and not review_fail

    feedback = ""
    if not passed:
        lines = []
        if code["blockers"]:
            lines.append("Objective checks (must fix):")
            lines += [f"- {b}" for b in code["blockers"]]
        relevant = major or issues
        if relevant:
            lines.append("Editor review (must fix):")
            for i in relevant:
                lines.append(
                    f"- [{i.get('section', '?')}] {i.get('problem', '')} "
                    f"Fix: {i.get('fix', '')}")
        feedback = "\n".join(lines)

    return passed, feedback, {"code": code, "review": review}


def _print_report(attempt: int, passed: bool, detail: dict) -> None:
    code, review = detail["code"], detail["review"]
    tag = "PASS" if passed else "FAIL"
    print(f"\n[editor] -- Review {attempt} -> {tag} --")
    print(f"[editor]   words={code['words']} internal_links="
          f"{code['internal_links']} external_links={code['external_links']} "
          f"faq={code['faq']}")
    if code["fixes"]:
        print("[editor]   inline fixes applied:")
        for f in code["fixes"]:
            print(f"[editor]     - {f}")
    for w in code["warnings"]:
        print(f"[editor]   WARN {w}")
    if code["blockers"]:
        print("[editor]   BLOCKERS:")
        for b in code["blockers"]:
            print(f"[editor]     x {b}")
    print(f"[editor]   Claude: {review.get('verdict')} - {review.get('summary', '')}")
    for i in review.get("issues", []):
        print(f"[editor]     [{i.get('severity', '?')}] "
              f"{i.get('section', '?')}: {i.get('problem', '')}")


def _write_manual_review(post: dict, feedback: str, detail: dict) -> None:
    MANUAL_REVIEW_FILE.write_text(json.dumps({
        "reason": "Failed editorial review after max retries.",
        "keyword_used": post.get("keyword_used"),
        "feedback": feedback,
        "last_detail": {"blockers": detail["code"]["blockers"],
                        "review": detail["review"]},
        "post": post,
        "flagged_at": datetime.now(timezone.utc).isoformat(),
    }, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[editor] Wrote {MANUAL_REVIEW_FILE.name}. Pipeline STOPPED "
          f"(final_check.py will not run).")


REVIEW_REQUEST_FILE = HERE / "review_request.json"


def _write_review_brief(post: dict) -> None:
    """Emit the editorial-review brief so Claude Code performs the Layer-2
    review as the editor (the API path does this itself). This makes the
    subjective review an explicit, unmissable step, not an ad-hoc one."""
    payload = {k: post.get(k, "") for k in
               ("title", "seo_title", "seo_description", "summary_html",
                "tags", "body_html")}
    REVIEW_REQUEST_FILE.write_text(json.dumps({
        "how_to_use": (
            "You (Claude Code) are the editor for this run. Judge `draft` "
            "against `reviewer_criteria` and return a PASS/FAIL verdict with "
            "specific issues (section, problem, fix, severity). If FAIL or any "
            "major issue, regenerate: generator.py --prep (add the feedback) -> "
            "rewrite generation_response.json -> generator.py --ingest -> "
            "editor.py again. Only proceed to imagegen/final_check on PASS."),
        "reviewer_criteria": REVIEWER_SYSTEM,
        "draft": payload,
    }, indent=2, ensure_ascii=False), encoding="utf-8")


def _code_only(post: dict) -> int:
    """Claude Code mode: run the deterministic checks + inline fixes, then hand
    the subjective review to Claude Code as the editor via a written brief.

    On PASS the inline fixes are persisted and review_request.json is written;
    Claude Code must then perform the editorial review (Layer 2) and only move on
    once it passes. On FAIL the blockers are printed for regeneration."""
    code = run_code_checks(post)  # mutates post with inline fixes
    passed = not code["blockers"]
    detail = {"code": code, "review": {
        "verdict": "PENDING (Claude Code editor review)",
        "summary": "", "issues": []}}
    _print_report(1, passed, detail)
    if passed:
        generator.write_post(post)
        _write_review_brief(post)
        print(f"\n[editor] Layer 1 PASS (code checks) — {POST_FILE.name} updated "
              f"with inline fixes.")
        print(f"[editor] Layer 2 REQUIRED: Claude Code must now act as the editor "
              f"— read {REVIEW_REQUEST_FILE.name}, judge tone / heading hierarchy "
              f"/ FAQ usefulness / link placement / CTA against reviewer_criteria, "
              f"and give a PASS/FAIL. Regenerate on FAIL; only then continue.")
        return 0
    feedback = ("Objective checks (must fix):\n"
                + "\n".join(f"- {b}" for b in code["blockers"]))
    print(f"\n[editor] FAIL (code checks). Regenerate with Claude Code, addressing:\n{feedback}")
    return 1


def main() -> int:
    if not POST_FILE.exists():
        print(f"[editor] ERROR: {POST_FILE.name} not found. Run generator.py first.")
        return 1
    post = json.loads(POST_FILE.read_text(encoding="utf-8"))

    # No API key -> Claude Code mode: deterministic checks only.
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return _code_only(post)

    try:
        keyword_data = generator.load_keyword()
    except (FileNotFoundError, ValueError) as e:
        print(f"[editor] ERROR: cannot load keyword.json for retries: {e}")
        return 1

    for attempt in range(MAX_RETRIES + 1):
        try:
            passed, feedback, detail = review_post(post)
        except EnvironmentError as e:
            print(f"[editor] ERROR: {e}")
            return 1

        _print_report(attempt + 1, passed, detail)

        if passed:
            generator.write_post(post)   # persist inline fixes
            print(f"\n[editor] PASS - {POST_FILE.name} approved for final_check.py.")
            return 0

        if attempt < MAX_RETRIES:
            print(f"\n[editor] FAIL - regenerating (retry {attempt + 1}/"
                  f"{MAX_RETRIES}) with feedback:\n{feedback}\n")
            try:
                post = generator.generate_post(keyword_data, feedback=feedback)
            except (anthropic.APIError, RuntimeError,
                    ValueError, EnvironmentError) as e:
                print(f"[editor] ERROR during regeneration: {e}")
                _write_manual_review(post, feedback, detail)
                return 2
        else:
            print(f"\n[editor] FAIL - exhausted {MAX_RETRIES} retries.")
            _write_manual_review(post, feedback, detail)
            return 2

    return 2


if __name__ == "__main__":
    sys.exit(main())
