"""
seo_check.py
------------
Step 3.3 of the pipeline: a deterministic SEO + AEO scorecard for post.json.

Runs locally, standard library only (no AI, no network), so it works in Claude
Code mode. It complements editor.py: the editor judges editorial *quality*;
this stage scores the on-page **SEO** signals (keyword placement, meta lengths,
links, headings, alt text) and the **AEO** signals (Answer Engine Optimisation
— valid FAQ schema, question-style headings, concise self-contained answers,
extractable structure) that make the article rank and get quoted by AI answer
engines and featured snippets.

Output: a printed scorecard + seo_report.json. BLOCKERS (return code 1) are
hard SEO/AEO failures that should be fixed before publishing; everything else is
a weighted score with warnings.

Usage:
  python seo_check.py            # score post.json
  # editor.py / run.py invoke it in sequence; also importable: score(post)
"""

import json
import re
import sys
from pathlib import Path

import common  # noqa: F401  (.env loader; also gives META_* limits)
from common import META_TITLE_MAX, META_DESC_MAX

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

HERE = Path(__file__).parent
POST = HERE / "post.json"
REPORT = HERE / "seo_report.json"

# Hard-minimum gate (empirically, a contract-compliant article scores 100/100
# on every keyword shape, so these floors are high on purpose). A miss signals a
# real contract violation to fix by regenerating, not an unavoidable penalty.
# Scoring granularity note: SEO has 15 checks so one soft warn = 97; AEO has 7
# checks so one soft warn = 93. Both floors permit exactly ONE soft warn and no
# hard fails (a hard AEO fail, e.g. missing/invalid FAQ schema, = 86 < 93; a hard
# SEO fail = 93 < 97). Empirically, genuine generation clusters at AEO 93 (a
# thorough-FAQ-answer warn), so 93 is the realistic floor; set 100 for zero
# tolerance (expect regen loops). SEO likewise: 100 for zero tolerance.
MIN_SEO_SCORE = 97    # one SEO soft-warn allowed, no hard fails
MIN_AEO_SCORE = 93    # one AEO soft-warn allowed, no hard fails

# Targets (soft unless noted).
MIN_WORDS = 1200
# Keyword density = (exact-phrase occurrences * keyword-word-count) / total words.
# Density-based (not an absolute occurrence cap) so a long article with naturally
# more mentions isn't penalised while genuine stuffing still warns. ~1-2% is
# ideal; outside the band warns, absent (0 occurrences) fails.
DENSITY_MIN, DENSITY_MAX = 0.5, 2.5   # percent
MIN_INTERNAL = 4
MIN_HEADING_KW = 2             # subheadings containing the keyword
MIN_QUESTION_HEADINGS = 3     # AEO: question-style H2/H3
FAQ_ANSWER_SNIPPET_MAX = 320  # chars; AEO answers should be snippet-sized


# ── text helpers ─────────────────────────────────────────────────────────────

def _no_scripts(html: str) -> str:
    return re.sub(r"<script\b.*?</script>", " ", html, flags=re.I | re.S)


def _text(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", _no_scripts(html))).strip()


def _headings(html: str) -> list[str]:
    return [re.sub(r"<[^>]+>", "", m).strip()
            for m in re.findall(r"<h[23]\b[^>]*>(.*?)</h[23]>", html, re.I | re.S)]


def _ldjson_blocks(html: str) -> list[str]:
    return re.findall(
        r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html, re.I | re.S)


def _check(label, status, detail):
    return {"label": label, "status": status, "detail": detail}  # status: pass|warn|fail


# ── scoring ──────────────────────────────────────────────────────────────────

def score(post: dict) -> dict:
    kw = str(post.get("keyword_used", "")).strip().lower()
    title = str(post.get("title", ""))
    seo_title = str(post.get("seo_title", ""))
    seo_desc = str(post.get("seo_description", ""))
    handle = str(post.get("handle", ""))
    body = str(post.get("body_html", ""))
    text = _text(body)
    low = text.lower()
    words = len(re.findall(r"\w+", text))
    headings = _headings(body)

    seo, aeo = [], []

    # ── SEO ──
    seo.append(_check("Keyword at start of SEO title",
        "pass" if seo_title.lower().startswith(kw) else
        ("warn" if kw in seo_title.lower() else "fail"),
        f"seo_title={seo_title!r}"))
    seo.append(_check("SEO title length <= %d" % META_TITLE_MAX,
        "pass" if 0 < len(seo_title) <= META_TITLE_MAX else "fail",
        f"{len(seo_title)} chars"))
    seo.append(_check("Keyword in meta description",
        "pass" if kw in seo_desc.lower() else "fail", f"desc={seo_desc[:60]!r}..."))
    seo.append(_check("Meta description length <= %d" % META_DESC_MAX,
        "pass" if 0 < len(seo_desc) <= META_DESC_MAX else "fail",
        f"{len(seo_desc)} chars"))
    seo.append(_check("Keyword in URL handle",
        "pass" if all(t in handle.lower() for t in kw.split()) else "fail",
        f"handle={handle!r}"))
    seo.append(_check("Keyword in H1 / title",
        "pass" if kw in title.lower() else
        ("warn" if all(t in title.lower() for t in kw.split()) else "fail"),
        f"title={title!r}"))
    first_para = (re.search(r"<p\b[^>]*>(.*?)</p>", body, re.I | re.S) or [None, ""])[1]
    first_low = re.sub(r"<[^>]+>", "", first_para).lower()
    seo.append(_check("Keyword in first paragraph",
        "pass" if kw in first_low else
        ("warn" if all(t in first_low for t in kw.split()) else "fail"),
        f"first para {len(first_low)} chars"))
    kw_headings = sum(1 for h in headings if kw in h.lower()
                      or all(t in h.lower() for t in kw.split()))
    seo.append(_check("Keyword in >= %d subheadings" % MIN_HEADING_KW,
        "pass" if kw_headings >= MIN_HEADING_KW else "warn",
        f"{kw_headings} of {len(headings)} subheadings"))
    occ = low.count(kw) if kw else 0
    density = (occ * len(kw.split()) / words * 100) if words else 0
    if occ == 0:
        kw_status = "fail"
    elif DENSITY_MIN <= density <= DENSITY_MAX:
        kw_status = "pass"
    else:
        kw_status = "warn"
    seo.append(_check("Keyword density (natural, not stuffed)", kw_status,
        f"{occ} occurrences, ~{density:.2f}% density "
        f"(target {DENSITY_MIN}-{DENSITY_MAX}%)"))
    seo.append(_check("Word count >= %d" % MIN_WORDS,
        "pass" if words >= MIN_WORDS else "warn", f"{words} words"))
    internal = len(re.findall(r'href=["\']/(?!/)', body))
    seo.append(_check("Internal links >= %d" % MIN_INTERNAL,
        "pass" if internal >= MIN_INTERNAL else "fail", f"{internal} internal links"))
    import config as cfg
    own = re.escape(cfg.SITE["site"])
    external = len(re.findall(r'href=["\']https?://(?![^"\']*' + own + r')', body, re.I))
    seo.append(_check("Outbound authority link >= 1",
        "pass" if external >= 1 else "warn", f"{external} external links"))
    imgs = re.findall(r"<img\b[^>]*>", body, re.I)
    no_alt = [t for t in imgs if not re.search(r'alt=["\'][^"\']', t, re.I)]
    seo.append(_check("All body images have alt text",
        "pass" if not no_alt else "fail", f"{len(imgs)} imgs, {len(no_alt)} missing alt"))
    hero_alt = next((p.get("alt", "") for p in post.get("image_prompts", [])
                     if (p.get("placement") or "").lower() == "hero"), "")
    seo.append(_check("Hero (featured) image alt has keyword",
        "pass" if kw and kw in hero_alt.lower() else "warn", f"hero alt={hero_alt[:50]!r}"))
    seo.append(_check("No stray <h1> in body (title is the H1)",
        "pass" if not re.search(r"<h1\b", body, re.I) else "fail", ""))

    # ── AEO (Answer Engine Optimisation) ──
    q_headings = sum(1 for h in headings if h.strip().endswith("?"))
    aeo.append(_check("Question-style headings >= %d" % MIN_QUESTION_HEADINGS,
        "pass" if q_headings >= MIN_QUESTION_HEADINGS else "warn",
        f"{q_headings} question headings"))
    # FAQ Q&A pairs.
    faq_pairs = re.findall(r"<h3\b[^>]*>(.*?)</h3>\s*<p\b[^>]*>(.*?)</p>", body, re.I | re.S)
    aeo.append(_check("FAQ present (>= 4 Q&A pairs)",
        "pass" if len(faq_pairs) >= 4 else "warn", f"{len(faq_pairs)} Q&A pairs"))
    # Valid FAQPage JSON-LD, mirroring the FAQ.
    faq_schema_ok, faq_detail = False, "no ld+json block"
    for blk in _ldjson_blocks(body):
        try:
            data = json.loads(blk)
        except (json.JSONDecodeError, ValueError):
            faq_detail = "ld+json present but invalid JSON"
            continue
        if isinstance(data, dict) and data.get("@type") == "FAQPage":
            n = len(data.get("mainEntity") or [])
            faq_schema_ok = n >= 4
            faq_detail = f"FAQPage with {n} questions"
            break
    aeo.append(_check("Valid FAQPage schema (JSON-LD)",
        "pass" if faq_schema_ok else "fail", faq_detail))
    # Snippet-sized answers (AEO: concise, self-contained).
    long_ans = [len(re.sub(r"<[^>]+>", "", a)) for _, a in faq_pairs
                if len(re.sub(r"<[^>]+>", "", a)) > FAQ_ANSWER_SNIPPET_MAX]
    aeo.append(_check("FAQ answers are snippet-sized (<= %d chars)" % FAQ_ANSWER_SNIPPET_MAX,
        "pass" if not long_ans else "warn",
        f"{len(long_ans)} of {len(faq_pairs)} answers over limit"))
    # Extractable structure: at least one list and one table.
    has_list = bool(re.search(r"<(ul|ol)\b", body, re.I))
    has_table = bool(re.search(r"<table\b", body, re.I))
    aeo.append(_check("Extractable structure (list + comparison table)",
        "pass" if has_list and has_table else "warn",
        f"list={has_list}, table={has_table}"))
    # Definitional / answer-first opener (entity clarity for LLMs).
    define = bool(kw and re.search(re.escape(kw) + r"\b[^.]{0,40}\b(is|are|means|refers)\b",
                                   first_low))
    aeo.append(_check("Answer-first / definitional opener",
        "pass" if define else "warn", "keyword defined early" if define else
        "no clear 'X is ...' near the top"))
    # Extractable summary/excerpt.
    aeo.append(_check("Summary/excerpt present",
        "pass" if str(post.get("summary_html", "")).strip() else "warn", ""))

    def pct(checks):
        pts = {"pass": 1.0, "warn": 0.5, "fail": 0.0}
        return round(sum(pts[c["status"]] for c in checks) / len(checks) * 100)

    blockers = [c["label"] for c in seo + aeo if c["status"] == "fail"]
    return {"seo": seo, "aeo": aeo, "seo_score": pct(seo), "aeo_score": pct(aeo),
            "blockers": blockers}


# ── report ───────────────────────────────────────────────────────────────────

_MARK = {"pass": "PASS", "warn": "warn", "fail": "FAIL"}


def _print(section: str, checks: list[dict]) -> None:
    print(f"\n  {section}")
    for c in checks:
        print(f"    [{_MARK[c['status']]:>4}] {c['label']}"
              + (f"  — {c['detail']}" if c["detail"] else ""))


def main() -> int:
    if not POST.exists():
        print("[seo_check] ERROR: post.json not found. Run generator + editor first.")
        return 1
    try:
        post = json.loads(POST.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, ValueError):
        print("[seo_check] ERROR: post.json unreadable.")
        return 1

    r = score(post)
    print("=" * 70)
    print(f"SEO / AEO SCORECARD — '{post.get('keyword_used','')}'")
    print("=" * 70)
    _print("SEO", r["seo"])
    _print("AEO (answer engines)", r["aeo"])
    print(f"\n  SEO score: {r['seo_score']}/100 (floor {MIN_SEO_SCORE})    "
          f"AEO score: {r['aeo_score']}/100 (floor {MIN_AEO_SCORE})")
    r["min_seo"], r["min_aeo"] = MIN_SEO_SCORE, MIN_AEO_SCORE
    REPORT.write_text(json.dumps(r, indent=2, ensure_ascii=False), encoding="utf-8")

    below = []
    if r["seo_score"] < MIN_SEO_SCORE:
        below.append(f"SEO {r['seo_score']} < {MIN_SEO_SCORE}")
    if r["aeo_score"] < MIN_AEO_SCORE:
        below.append(f"AEO {r['aeo_score']} < {MIN_AEO_SCORE}")
    if r["blockers"] or below:
        if r["blockers"]:
            print(f"\n[seo_check] {len(r['blockers'])} blocker(s): "
                  + "; ".join(r["blockers"]))
        if below:
            print(f"[seo_check] below floor: {'; '.join(below)}")
        print("[seo_check] Fix these before publishing (regenerate if needed).")
        return 1
    print("\n[seo_check] PASS — both scores meet the floor. Scorecard -> seo_report.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
