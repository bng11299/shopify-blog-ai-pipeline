"""
final_check.py
--------------
Step 3.9 of the pipeline: the gate between editor approval and publishing.

Runs locally — no Shopify/AI connection, standard library only. It:
  1. Re-validates the approved post.json one last time (required fields, both
     image placeholders present, SEO meta lengths).
  2. Confirms both images exist (images/manifest.json) with alt text, so the
     publish won't stall halfway through.
  3. Prints the ordered PUBLISH PLAN (see docs/PUBLISHING.md), so the Shopify
     steps are reproduced identically every run.
  4. With --mark-posted, records the keyword in posted_keywords.json so the
     scraper never re-picks the topic. Run that AFTER the draft is confirmed
     created (idempotent — it never adds a duplicate).

Usage:
  python final_check.py                 # validate + print the publish plan
  python final_check.py --mark-posted   # also mark the keyword as done
"""

import json
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

HERE = Path(__file__).parent
POST = HERE / "post.json"
MANIFEST = HERE / "images" / "manifest.json"
POSTED_LOG = HERE / "posted_keywords.json"
PUBLISHING_DOC = "docs/PUBLISHING.md"

# Fixed pipeline contract — the tokens generator emits and publisher swaps.
HERO_TOKEN = "{{HERO_IMAGE}}"
INBODY_TOKEN = "{{INBODY_IMAGE}}"
META_TITLE_MAX = 60
META_DESC_MAX = 160
REQUIRED = ("title", "handle", "seo_title", "seo_description", "summary_html",
            "tags", "body_html", "image_prompts", "keyword_used")


def _load(path: Path):
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, ValueError):
        return None


def validate(post: dict, manifest: dict | None) -> tuple[list[str], list[str]]:
    """Return (blockers, warnings). Blockers mean do NOT publish yet."""
    blockers, warnings = [], []

    for k in REQUIRED:
        if not post.get(k):
            blockers.append(f"post.json missing required field: {k}")
    body = post.get("body_html", "")
    # The hero is the article's featured image (set by the publisher), not in the
    # body; only the in-body placeholder must be present.
    if INBODY_TOKEN not in body:
        blockers.append(f"body_html missing in-body placeholder {INBODY_TOKEN}")

    if len(post.get("seo_title", "")) > META_TITLE_MAX:
        warnings.append(f"seo_title {len(post['seo_title'])}>{META_TITLE_MAX}")
    if len(post.get("seo_description", "")) > META_DESC_MAX:
        warnings.append(
            f"seo_description {len(post['seo_description'])}>{META_DESC_MAX}")

    if not manifest or "images" not in manifest:
        blockers.append("images/manifest.json missing — run imagegen.py")
    else:
        imgs = manifest["images"]
        for placement in ("hero", "in-body"):
            entry = imgs.get(placement)
            if not entry:
                blockers.append(f"no {placement} image in manifest")
                continue
            fpath = HERE / "images" / entry.get("file", "")
            if not entry.get("file") or not fpath.exists():
                blockers.append(f"{placement} image file missing on disk: "
                                f"{entry.get('file')!r}")
            if not entry.get("alt"):
                warnings.append(f"{placement} image has no alt text")

    return blockers, warnings


def mark_posted(keyword: str) -> str:
    """Add keyword to posted_keywords.json (idempotent). Returns a status line."""
    kw = (keyword or "").strip()
    if not kw:
        return "no keyword_used to record"
    existing = _load(POSTED_LOG) or []
    if not isinstance(existing, list):
        existing = []
    lowered = {str(k).lower().strip() for k in existing}
    if kw.lower() in lowered:
        return f"'{kw}' already in {POSTED_LOG.name} (no change)"
    existing.append(kw)
    POSTED_LOG.write_text(json.dumps(existing, indent=2, ensure_ascii=False)
                          + "\n", encoding="utf-8")
    return f"recorded '{kw}' in {POSTED_LOG.name}"


def print_plan(post: dict, manifest: dict) -> None:
    handle = post.get("handle", "")
    imgs = manifest.get("images", {})
    hero = imgs.get("hero", {}).get("file", "?")
    inbody = imgs.get("in-body", {}).get("file", "?")
    print("\n" + "=" * 70)
    print(f"PUBLISH PLAN — '{post.get('keyword_used','')}'  (handle: {handle})")
    print("=" * 70)
    print(f"Full runbook: {PUBLISHING_DOC}\n")
    print("Ordered steps (publisher.py drives the GraphQL Admin API):")
    print("  1. python publisher.py --check       (verify token + resolve blog GID)")
    print("  2. python publisher.py               (DRY RUN — review the planned input)")
    print(f"       hero image:    images/{hero}")
    print(f"       in-body image: images/{inbody}")
    print("  3. python publisher.py --publish     (upload images, create DRAFT article,")
    print("                                        set global/title_tag + description_tag)")
    print("  4. Review the DRAFT in Shopify admin (Blog posts) and publish it there.")
    print("  5. python final_check.py --mark-posted   (after the draft exists)")
    print(f"\n  Handle note: if '/{handle}' already exists on the blog, Shopify will")
    print("  reject or suffix it — rename in post.json or the admin if so.")
    print("=" * 70 + "\n")


def main() -> int:
    post = _load(POST)
    if post is None:
        print("[final_check] ERROR: post.json not found or unreadable. Run the "
              "generator + editor first.")
        return 1
    manifest = _load(MANIFEST)

    blockers, warnings = validate(post, manifest)
    for w in warnings:
        print(f"[final_check] WARNING: {w}")
    if blockers:
        for b in blockers:
            print(f"[final_check] BLOCKER: {b}")
        print(f"[final_check] NOT ready to publish ({len(blockers)} blocker(s)).")
        return 1

    print(f"[final_check] post.json is publishable "
          f"('{post.get('keyword_used','')}', handle '{post.get('handle','')}').")

    if "--mark-posted" in sys.argv[1:]:
        print(f"[final_check] {mark_posted(post.get('keyword_used', ''))}")
    else:
        print_plan(post, manifest or {"images": {}})
    return 0


if __name__ == "__main__":
    sys.exit(main())
