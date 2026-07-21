"""
generator.py
------------
Step 2 of the pipeline.

Reads keyword.json (from scraper.py), calls the Anthropic Messages API to write
a complete, buyer-focused Shopify blog article for the configured store
(config.py / config_local.py), and writes post.json for editor.py to review.

Text generation is Claude (not Gemini). The article JSON shape is GUARANTEED by
a forced **tool_use** call with a strict input_schema — the model must return a
tool call whose input matches the schema, so we never parse free-form JSON or
fight truncated fences. (See the claude-api skill; strict tool use validates at
the API layer and the model retries on mismatch.)

Output (post.json) fields — plain semantic HTML, no WPBakery, no RankMath:
  title, body_html, summary_html, tags, handle, seo_title, seo_description,
  image_prompts[{placement, prompt}], keyword_used, generated_at

body_html is clean semantic HTML: the hero <img> first, then the article, an
inline comparison <table> and an inline FAQ, ending with a FAQPage JSON-LD
<script> block (FAQ rich results). It does NOT add BlogPosting/Article schema
when config.SHOPIFY["theme_emits_article_schema"] is True — most themes emit
that already and duplicating it conflicts.

Two ways to run, chosen automatically by whether ANTHROPIC_API_KEY is set:

  A) Claude Code mode (DEFAULT when no ANTHROPIC_API_KEY) — this Claude Code
     session is the writer, no API key needed:
       python generator.py --prep     # -> generation_request.json (prompt+schema)
       # ...Claude Code reads it and writes the article JSON to
       #    generation_response.json...
       python generator.py --ingest    # validate that JSON -> post.json

  B) API mode (when ANTHROPIC_API_KEY is set) — forced tool_use, fully scripted:
       python generator.py             # keyword.json -> post.json

  editor.py imports generate_post(keyword_data, feedback=...) for API-mode regen.

Requires:
  pip install anthropic   (import-only in Claude Code mode; the API is not called)
  internal_links.json (from build_internal_links.py) for real internal links.
"""

import json
import os
import re
import sys
from datetime import datetime, timezone
from html import escape
from pathlib import Path

import anthropic

import config as cfg
import common
from common import (HERO_IMG, INBODY_IMG, META_TITLE_MAX, META_DESC_MAX,
                    KEYWORD_IN, POST_OUT, load_links, select_internal_links,
                    format_links, trim_meta, slugify, space_blocks)

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

# ── Config ──────────────────────────────────────────────────────────────────

# Default to the latest, most capable Claude model. Override with GENERATOR_MODEL.
MODEL = os.environ.get("GENERATOR_MODEL", "claude-opus-4-8")
# Article body + tool JSON is long; stream so a large budget can't hit the
# SDK's non-streaming HTTP-timeout guard.
MAX_TOKENS = 16000

BRAND = cfg.SITE
SERVICE_PAGES = cfg.SERVICE_PAGES
CORE_HUBS = cfg.CORE_HUBS

REQUIRED_KEYS = {"title", "body_html", "summary_html", "tags", "handle",
                 "seo_title", "seo_description", "image_prompts"}


# ── Article tool (forced structured output) ──────────────────────────────────
# strict=True guarantees tool_use.input validates exactly against this schema.
# Strict mode requires additionalProperties:false + required on every object and
# forbids length/count constraints (min/maxItems, min/maxLength) — those are
# checked in _validate() below instead.

ARTICLE_TOOL = {
    "name": "emit_article",
    "description": "Return the finished Shopify blog article as structured data.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "title": {"type": "string", "description": "The article title (the H1)."},
            "handle": {"type": "string",
                       "description": "URL handle: focus keyword, lowercase, hyphenated."},
            "seo_title": {"type": "string",
                          "description": f"SEO title tag, <= {META_TITLE_MAX} chars, keyword first."},
            "seo_description": {"type": "string",
                                "description": f"SEO meta description, <= {META_DESC_MAX} chars."},
            "summary_html": {"type": "string",
                             "description": "2-3 sentence excerpt for the blog index (plain <p> ok)."},
            "tags": {"type": "array", "items": {"type": "string"},
                     "description": "4-6 topical tags."},
            "image_prompts": {
                "type": "array",
                "description": "EXACTLY 2 image prompts: one hero, one in-body.",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "placement": {"type": "string", "enum": ["hero", "in-body"]},
                        "prompt": {"type": "string",
                                   "description": f"Detailed prompt, {cfg.SITE['image_context']}; any people are Singaporean/SE-Asian; no text in image."},
                        "alt": {"type": "string",
                                "description": "Alt text for this image; the hero alt must contain the focus keyword."},
                    },
                    "required": ["placement", "prompt", "alt"],
                },
            },
            "body_html": {"type": "string",
                          "description": "The full article as clean semantic HTML per the rules."},
        },
        "required": ["title", "handle", "seo_title", "seo_description",
                     "summary_html", "tags", "image_prompts", "body_html"],
    },
}


# ── Prompt construction ─────────────────────────────────────────────────────

def _schema_note() -> str:
    """Whether to instruct the model to emit an Article/BlogPosting JSON-LD block."""
    if cfg.SHOPIFY.get("theme_emits_article_schema", True):
        return ("Do NOT add any BlogPosting/Article JSON-LD schema — the Shopify "
                "theme already emits it and a second block conflicts.")
    return ("Add a single BlogPosting JSON-LD <script type=\"application/ld+json\"> "
            "block at the end of the body (the theme does not emit one).")


SYSTEM_PROMPT = f"""You are a senior e-commerce content writer and on-page SEO \
specialist for {BRAND['company']} ({BRAND['site']}), {BRAND['descriptor']}

You write buyer-focused blog articles that rank for the target keyword, \
establish {BRAND['short_name']} as a trusted expert, and drive enquiries and \
sales. This is for the Shopify store at {BRAND['site']} ONLY.

HARD RULES
- Write for {BRAND['audience']}. Use {BRAND['spelling']}.
- Helpful and educational first; never a hard-sell. Calls to action point to \
browsing/enquiry, never fake urgency.
- Never sound AI-generated. BANNED phrases: "in today's fast-paced world", \
"it's worth noting", "delve", "game-changer", "in conclusion", "unlock", \
"navigate the landscape", "ever-evolving", "when it comes to", "in the realm of".
- Mix short punchy sentences with longer explanatory ones. No corporate waffle.
- Avoid em-dashes (the "--"/long dash). Use commas, full stops, or brackets \
instead. At most ONE em-dash in the entire article.

ON-PAGE SEO (Shopify — there is no RankMath score; aim for these heuristics)
- Put the EXACT focus keyword at the START of seo_title, and include it in \
seo_description, summary_html, the H1, the FIRST sentence of the intro, the \
handle, and in at least TWO H2/H3 subheadings.
- Use the focus keyword and close variants naturally (~8-14 times); never stuff.
- seo_title <= {META_TITLE_MAX} characters, keyword-first.
- seo_description <= {META_DESC_MAX} characters, action-oriented, contains the keyword.
- handle: the focus keyword, lowercase, hyphenated. No stop-word padding.
- Every <img> MUST have an alt attribute; the HERO image alt MUST contain the \
focus keyword.
- Include AT LEAST ONE outbound link to an authoritative external source — \
{BRAND['authority_desc']} — as <a href="..." target="_blank" rel="noopener">.
- Include AT LEAST FOUR internal links to real store paths (allow-list below).
- Length: 1,500-2,400 words of substantive, specific copy.

HTML (body_html — clean semantic HTML only; no markdown, no \
<html>/<head>/<body>, no inline styles, no shortcodes)
- Do NOT put the hero/banner image in body_html — the Shopify theme shows the \
article's featured image above the content, so a hero inside the body would \
duplicate it. Include EXACTLY ONE in-body image at a natural mid-article break: \
`<img src="{INBODY_IMG}" alt="<descriptive alt>">`. (The publisher swaps this \
placeholder for the uploaded URL; the hero is generated and set as the featured \
image separately.)
- Use <h2>/<h3> headings (do NOT include an <h1> in body_html — the article \
title is the H1, rendered by the theme). Short paragraphs (2-4 sentences), \
<ul>/<li> lists where helpful.
- Open the intro's FIRST sentence with a plain definition of the topic — \
"<focus keyword> is ..." — so the term is defined up front for readers and \
answer engines, then continue the intro.
- Include AT LEAST ONE clean comparison <table> where natural (e.g. option A vs \
option B, or a feature/tier comparison), using \
<table><thead><tr><th>...</th></tr></thead><tbody><tr><td>...</td></tr></tbody></table>.
- End the body with a Frequently Asked Questions section: an <h2>Frequently \
asked questions</h2> then 6-8 Q&A pairs, EACH as `<h3>Question?</h3>` \
immediately followed by `<p>Answer.</p>`. Keep EACH answer concise and \
self-contained — a direct answer in 1-3 sentences, UNDER ~50 words / 320 \
characters — so an answer engine can lift it verbatim as a snippet. Put any \
extra nuance in the article body, not the FAQ answer.
- After the FAQ, append ONE `<script type="application/ld+json">` block with a \
schema.org FAQPage whose mainEntity mirrors the FAQ Q&As exactly (for FAQ rich \
results). {_schema_note()}
- Put a blank line between block-level elements so the raw HTML is cleanly spaced.
- Do NOT write a contact/enquiry block, phone number, email, address, an \
"Est." line, or a "Related reading" list. The store's contact CTA and related \
links are appended automatically after your output — end body_html with the \
FAQPage script.

Return the article by calling the `emit_article` tool. Every field is \
mandatory. image_prompts must contain EXACTLY 2 items — one "hero" (the \
banner/featured image) and one "in-body" (illustrates a point in the article) — \
each with a detailed `prompt` and `alt` text. The hero `alt` MUST contain the \
focus keyword. When people appear, make them the FOREGROUND subject in sharp \
focus (not background extras): authentic Singaporean professionals reflecting \
the local mix (Chinese, Malay or Indian), genuinely engaged with the task, in a \
real local business setting. Aim for candid, editorial quality, not generic \
glossy stock."""


def _angle_instruction(keyword_data: dict) -> str:
    url = (keyword_data.get("url_found") or "").strip()
    if url and url.upper() != "NOT FOUND":
        return (
            f"\n\nIMPORTANT — an existing store page already ranks for this topic "
            f"at `{url}`. Do NOT duplicate a product/collection page. Write a "
            f"COMPLEMENTARY blog angle (a practical buyer's guide, how-to-choose, "
            f"checklist, comparison, or common-mistakes piece) that adds new value "
            f"and links to `{url}` as the related page."
        )
    return ""


def build_user_message(keyword_data: dict, links: list[dict],
                       feedback: str | None = None) -> str:
    kw = keyword_data["keyword"]
    chosen = select_internal_links(kw, keyword_data.get("url_found", ""), links)
    example_path = next(iter(SERVICE_PAGES), "/collections/all")
    msg = (
        f"Write a {BRAND['short_name']} blog article targeting the primary/focus "
        f"keyword: \"{kw}\".\n"
        f"Current SerpReport rank: position "
        f"{keyword_data.get('position', 'n/a')} (local search volume "
        f"~{keyword_data.get('local_vol', 'n/a')}/mo). Goal: climb from the "
        f"8-12 band onto page 1.\n"
        f"Use \"{kw}\" as the focus keyword throughout.\n\n"
        f"INTERNAL LINK ALLOW-LIST — link relevant anchor text ONLY to these real "
        f"{BRAND['site']} paths (relative, e.g. "
        f"<a href=\"{example_path}\">anchor text</a>). Do NOT invent any other "
        f"internal URL:\n{format_links(chosen)}"
    )
    msg += _angle_instruction(keyword_data)
    if feedback:
        msg += (
            "\n\nA previous draft was REJECTED in review. Address every point "
            "below in this new version:\n" + feedback.strip()
        )
    return msg


# ── Generation ──────────────────────────────────────────────────────────────

def _extract_tool_input(message) -> dict:
    """Pull the emit_article tool_use input out of a finished message."""
    for block in message.content:
        if block.type == "tool_use" and block.name == "emit_article":
            return dict(block.input)
    raise RuntimeError("model did not call emit_article (no tool_use block found)")


def _validate(post: dict, keyword: str) -> None:
    post["handle"] = slugify(post.get("handle") or keyword)

    if len(post["seo_title"]) > META_TITLE_MAX:
        print(f"[generator] seo_title {len(post['seo_title'])}>{META_TITLE_MAX}; trimming.")
    post["seo_title"] = trim_meta(post["seo_title"], META_TITLE_MAX)
    if len(post["seo_description"]) > META_DESC_MAX:
        print(f"[generator] seo_description {len(post['seo_description'])}>{META_DESC_MAX}; trimming.")
    post["seo_description"] = trim_meta(post["seo_description"], META_DESC_MAX)

    post["body_html"] = space_blocks(post["body_html"])

    n_imgs = len(post.get("image_prompts", []))
    if n_imgs != 2:
        print(f"[generator] WARNING: expected 2 image_prompts, got {n_imgs}.")
    if not post.get("tags"):
        print("[generator] WARNING: no tags returned.")

    body = post["body_html"]
    low = body.lower()
    kw = keyword.lower()
    if "<table" not in low:
        print("[generator] WARNING: no comparison <table> found.")
    own_domain = re.escape(cfg.SITE["site"])
    if not re.search(r'<a\s+[^>]*href=["\']https?://(?!'
                     r'[^"\']*' + own_domain + r')', body, re.I):
        print("[generator] WARNING: no external authority link found.")
    if len(re.findall(r'href=["\']/', body)) < 4:
        print("[generator] WARNING: fewer than 4 internal links found.")
    hero_alt = next((p.get("alt", "") for p in post.get("image_prompts", [])
                     if (p.get("placement") or "").lower() == "hero"), "")
    if hero_alt and kw not in hero_alt.lower():
        print("[generator] WARNING: focus keyword not in hero image alt (image_prompts).")


# ── Closing block (CTA + related links) — built in Python, appended to body ──
# The store's template article ends: FAQ -> contact CTA
# (phone/WhatsApp/email/address, Est. year) -> Related links. Contact details are
# brand constants from config (never LLM-written); related links are real
# allow-listed store URLs.

def _title_words(text: str) -> str:
    return " ".join(w.upper() if w.lower() in cfg.ACRONYMS else w.capitalize()
                    for w in text.split())


def build_closing(keyword_data: dict, links: list[dict]) -> str:
    kw = keyword_data.get("keyword", "")
    s = cfg.SITE
    url_found = (keyword_data.get("url_found") or "").strip()
    shop_url = url_found if url_found and url_found.upper() != "NOT FOUND" else cfg.CONTACT_PATH
    shop_label = next((l.get("title") for l in links if l["url"] == shop_url), None) \
        or "our range"

    cta = [f"<h2>Need help with {_title_words(kw)}? Talk to {escape(s['short_name'])}</h2>",
           f"<p>{escape(s['short_name'])} supplies and supports business IT for "
           f"{escape(s['region'])} companies, from choosing the right model to setup, "
           f"backup and warranty. Tell us your headcount and what you store, and we "
           f"will recommend an option.</p>"]
    items = [f'<li>Browse <a href="{shop_url}">{escape(shop_label)}</a></li>']
    if s.get("phone"):
        digits = re.sub(r"[^0-9+]", "", s["phone"])
        wa = " or WhatsApp" if s.get("whatsapp") else ""
        items.append(f'<li>Call{wa} us on <a href="tel:{digits}">{escape(s["phone"])}</a></li>')
    if s.get("email"):
        items.append(f'<li>Email <a href="mailto:{s["email"]}">{escape(s["email"])}</a></li>')
    cta.append("<ul>\n" + "\n".join(items) + "\n</ul>")
    # Escape each part, then join with the middot entity (do NOT escape the entity).
    bits = [escape(x) for x in (s.get("company"), s.get("address"),
            f"Est. {s['established']}" if s.get("established") else "") if x]
    if bits:
        cta.append("<p>" + " &middot; ".join(bits) + "</p>")

    # Related reading: prefer real blog articles from the allow-list.
    chosen = select_internal_links(kw, url_found, links, n=12)
    rel, seen = [], {shop_url}
    for l in chosen:
        u = l["url"]
        if u in seen or not u.startswith("/blogs/"):
            continue
        rel.append(f'<li><a href="{u}">{escape(l.get("title") or u)}</a></li>')
        seen.add(u)
        if len(rel) >= 5:
            break
    related = ("<h2>Related reading</h2>\n<ul>\n" + "\n".join(rel) + "\n</ul>") if rel else ""

    return "\n\n".join(cta + ([related] if related else []))


def generate_post(keyword_data: dict, feedback: str | None = None) -> dict:
    """Generate a full article dict from a keyword record.

    editor.py calls this with `feedback` to regenerate a rejected draft.
    """
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise EnvironmentError("ANTHROPIC_API_KEY is not set in the environment.")
    client = anthropic.Anthropic()

    links = load_links()
    user_message = build_user_message(keyword_data, links, feedback)

    print(f"[generator] Generating article for '{keyword_data['keyword']}' "
          f"using {MODEL}{' (with editor feedback)' if feedback else ''}...")

    last_err = None
    attempts = 3
    for attempt in range(1, attempts + 1):
        try:
            with client.messages.stream(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                system=SYSTEM_PROMPT,
                tools=[ARTICLE_TOOL],
                tool_choice={"type": "tool", "name": "emit_article"},
                messages=[{"role": "user", "content": user_message}],
            ) as stream:
                message = stream.get_final_message()

            if message.stop_reason == "max_tokens":
                raise RuntimeError("output hit max_tokens — article truncated")
            post = _extract_tool_input(message)
            missing = REQUIRED_KEYS - set(post)
            if missing:
                raise ValueError(f"missing keys: {sorted(missing)}")
            body = post.get("body_html", "")
            if INBODY_IMG not in body:
                raise ValueError("body_html missing in-body image placeholder")
            break
        except (ValueError, RuntimeError) as e:
            last_err = e
            if attempt == attempts:
                raise RuntimeError(f"Generation failed after {attempts} attempts: {e}") from e
            print(f"[generator] Attempt {attempt} failed ({e}); retrying...")
            user_message += (
                f"\n\nYour previous output was invalid: {e}. Call emit_article "
                f"again with ALL required fields, the in-body image placeholder "
                f"({INBODY_IMG}) present in body_html (NO hero image in the body), "
                f"and EXACTLY 2 image_prompts each with placement, prompt and alt.")

    _validate(post, keyword_data["keyword"])
    post["body_html"] = post["body_html"] + "\n\n" + build_closing(keyword_data, links)
    post["keyword_used"] = keyword_data["keyword"]
    post["generated_at"] = datetime.now(timezone.utc).isoformat()

    try:
        words = len(re.findall(r"\w+", re.sub(r"<[^>]+>", " ", post["body_html"])))
        print(f"[generator] Done. ~{words} words, {len(post['body_html'])} chars HTML.")
    except (AttributeError, TypeError):
        print(f"[generator] Done. {len(post['body_html'])} chars HTML.")
    return post


# ── I/O ─────────────────────────────────────────────────────────────────────

def load_keyword() -> dict:
    if not KEYWORD_IN.exists():
        raise FileNotFoundError(f"{KEYWORD_IN.name} not found. Run scraper.py first.")
    data = json.loads(KEYWORD_IN.read_text(encoding="utf-8"))
    if not data.get("keyword"):
        raise ValueError(f"{KEYWORD_IN.name} has no 'keyword' field.")
    return data


def write_post(post: dict) -> None:
    POST_OUT.write_text(json.dumps(post, indent=2, ensure_ascii=False),
                        encoding="utf-8")
    print(f"[generator] Written to {POST_OUT.name}")


# ── Claude Code mode (no ANTHROPIC_API_KEY: this session is the writer) ──────

REQUEST_FILE = POST_OUT.with_name("generation_request.json")
RESPONSE_FILE = POST_OUT.with_name("generation_response.json")


def build_generation_request(keyword_data: dict, feedback: str | None = None) -> dict:
    """Assemble the full prompt + schema for Claude Code to write against."""
    links = load_links()
    return {
        "keyword": keyword_data["keyword"],
        "how_to_use": (
            "You (Claude Code) are the writer. Produce ONE JSON object that "
            "validates against `schema` (the emit_article tool input), following "
            "`system_prompt` and `user_message` exactly. Save it as "
            "generation_response.json, then run:  python generator.py --ingest"),
        "system_prompt": SYSTEM_PROMPT,
        "user_message": build_user_message(keyword_data, links, feedback),
        "schema": ARTICLE_TOOL["input_schema"],
    }


def do_prep(feedback: str | None = None) -> int:
    kw = load_keyword()
    REQUEST_FILE.write_text(
        json.dumps(build_generation_request(kw, feedback), indent=2, ensure_ascii=False),
        encoding="utf-8")
    print("[generator] Claude Code mode (no ANTHROPIC_API_KEY).")
    print(f"[generator] Wrote {REQUEST_FILE.name} for keyword '{kw['keyword']}'.")
    print("[generator] Next: have Claude Code read that file, write the article "
          "JSON to")
    print(f"[generator]   {RESPONSE_FILE.name}, then run:  python generator.py --ingest")
    return 0


def do_ingest(path: Path | None = None) -> int:
    src = path or RESPONSE_FILE
    if not src.exists():
        print(f"[generator] ERROR: {src.name} not found. Run --prep first, then "
              f"have Claude Code write the article JSON there.")
        return 1
    try:
        post = json.loads(src.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, ValueError) as e:
        print(f"[generator] ERROR: {src.name} is not valid JSON: {e}")
        return 1
    if not isinstance(post, dict):
        print(f"[generator] ERROR: {src.name} must be a single JSON object.")
        return 1
    missing = REQUIRED_KEYS - set(post)
    if missing:
        print(f"[generator] ERROR: response missing required keys: {sorted(missing)}")
        return 1
    body = post.get("body_html", "")
    if INBODY_IMG not in body:
        print(f"[generator] ERROR: body_html missing {INBODY_IMG} "
              f"(the hero must NOT be in the body; it becomes the featured image).")
        return 1

    try:
        kw_data = load_keyword()
        keyword = kw_data["keyword"]
    except (FileNotFoundError, ValueError):
        kw_data = {"keyword": post.get("keyword_used", ""), "url_found": ""}
        keyword = kw_data["keyword"]

    _validate(post, keyword)
    post["body_html"] = post["body_html"] + "\n\n" + build_closing(kw_data, load_links())
    post["keyword_used"] = keyword
    post["generated_at"] = datetime.now(timezone.utc).isoformat()
    write_post(post)
    words = len(re.findall(r"\w+", re.sub(r"<[^>]+>", " ", post["body_html"])))
    print(f"[generator] Ingested + validated. ~{words} words, "
          f"{len(post['body_html'])} chars HTML.")
    print(f"[generator]   handle: {post['handle']}  "
          f"seo_title({len(post['seo_title'])}c): {post['seo_title']}")
    return 0


# ── API mode (ANTHROPIC_API_KEY set) ─────────────────────────────────────────

def _api_generate() -> int:
    try:
        keyword_data = load_keyword()
    except (FileNotFoundError, ValueError) as e:
        print(f"[generator] ERROR: {e}")
        return 1
    try:
        post = generate_post(keyword_data)
    except EnvironmentError as e:
        print(f"[generator] ERROR: {e} Set ANTHROPIC_API_KEY and retry.")
        return 1
    except anthropic.APIError as e:
        print(f"[generator] ERROR: Anthropic API rejected the request: {e}")
        return 1
    except (RuntimeError, ValueError) as e:
        print(f"[generator] ERROR during generation: {e}")
        return 1
    write_post(post)
    print(f"\n[generator] Title: {post['title']}")
    print(f"[generator]   handle: {post['handle']}")
    print(f"[generator]   seo_title ({len(post['seo_title'])}c): {post['seo_title']}")
    print(f"[generator]   seo_description ({len(post['seo_description'])}c): {post['seo_description']}")
    print(f"[generator]   tags: {', '.join(post['tags'])}")
    return 0


# ── Entry point ─────────────────────────────────────────────────────────────

def main() -> int:
    args = sys.argv[1:]
    if "--ingest" in args:
        idx = args.index("--ingest")
        p = (args[idx + 1] if idx + 1 < len(args)
             and not args[idx + 1].startswith("-") else None)
        return do_ingest(Path(p) if p else None)
    if "--prep" in args or not os.environ.get("ANTHROPIC_API_KEY"):
        try:
            return do_prep()
        except (FileNotFoundError, ValueError) as e:
            print(f"[generator] ERROR: {e}")
            return 1
    return _api_generate()


if __name__ == "__main__":
    sys.exit(main())
