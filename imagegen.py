"""
imagegen.py
-----------
Step 3.5 of the pipeline: generate the article's two images with Gemini's image
model ("nano banana"), using the image_prompts that generator.py produced and
the alt text already embedded in body_html.

Every image passes a "photo editor" QA gate: a vision model combs it for AI
artifacts (garbled text, bad hands, holograms, warped geometry, uncanny faces)
and unsuitable images are regenerated. After 3 consecutive rejections the
prompt itself is revised from the reviewer's notes; hard-fails after 3 prompts
(9 generations). The manifest records the accepted prompt and the review log.

Reads  post.json  ->  writes  images/<handle>-hero.png, images/<handle>-in-body.png
and images/manifest.json ({placement: {file, alt, prompt}}) for publisher.py to
upload to Shopify (Files API: stagedUploadsCreate -> PUT bytes -> fileCreate)
and swap into body_html / set as the article image.

Usage:
  python imagegen.py            # uses post.json
  python imagegen.py other.json

Requires:
  pip install google-genai
  GEMINI_API_KEY in the environment or the project .env (same loader as
  common.py; key value is never printed).
"""

import json
import os
import re
import sys
from pathlib import Path

from google import genai
from google.genai import types as genai_types

import common       # shared helpers + .env loader (runs at import)
import config as cfg

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

MODEL = os.environ.get("IMAGE_MODEL", "gemini-2.5-flash-image")
REVIEW_MODEL = os.environ.get("IMAGE_REVIEW_MODEL", "gemini-2.5-flash")
HERE = Path(__file__).parent
OUT_DIR = HERE / "images"

# Photo-editor QA loop: up to ATTEMPTS_PER_PROMPT generations per prompt; after
# that many consecutive rejections the prompt is revised (using the reviewer's
# artifact notes) and the counter resets, up to MAX_PROMPTS prompts total.
ATTEMPTS_PER_PROMPT = 3
MAX_PROMPTS = 3

# House style appended to every prompt — keeps outputs on-brand and usable.
STYLE_SUFFIX = (
    " Authentic editorial photography with a candid, documentary feel and "
    f"natural lighting; {cfg.SITE['image_context']}. Avoid the generic glossy "
    "stock-photo look. When people appear they are the clear FOREGROUND subject "
    "in sharp focus (not blurred background extras): real Singaporean "
    "professionals reflecting Singapore's local mix (Chinese, Malay or Indian), "
    "aged 30s to 40s, in smart office attire, genuinely engaged with the task and "
    "at ease on camera. Absolutely no text, no words, no letters, no logos, no "
    "watermarks anywhere in the image. No floating holograms, augmented-reality "
    "overlays, or projected user-interface graphics; only real physical objects "
    "and ordinary screens."
)

ASPECT_RATIO = "16:9"  # landscape banners

_EXT = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}


def _alt_for(body: str, token: str) -> str:
    """Pull the alt text off the <img> tag carrying the given src placeholder."""
    m = re.search(r"<img\b[^>]*?" + re.escape(token) + r"[^>]*?>", body, re.I)
    if not m:
        return ""
    alt = re.search(r'alt=["\'](.*?)["\']', m.group(0), re.I | re.S)
    return alt.group(1).strip() if alt else ""


def _generate_one(client: genai.Client, prompt: str, out_stem: Path) -> Path:
    """Call the image model and write the first returned image to disk."""
    try:
        config = genai_types.GenerateContentConfig(
            image_config=genai_types.ImageConfig(aspect_ratio=ASPECT_RATIO))
    except (AttributeError, TypeError):
        config = None  # older SDK without ImageConfig — square fallback
    last_err = None
    for attempt in (1, 2, 3):
        try:
            resp = client.models.generate_content(
                model=MODEL, contents=prompt + STYLE_SUFFIX, config=config)
            for cand in (resp.candidates or []):
                for part in (cand.content.parts or []):
                    blob = getattr(part, "inline_data", None)
                    if blob and blob.data:
                        path = out_stem.with_suffix(
                            _EXT.get(blob.mime_type, ".png"))
                        path.write_bytes(blob.data)
                        return path
            raise RuntimeError("response contained no image data")
        except Exception as e:  # SDK raises several transport/typing errors
            last_err = e
            print(f"[imagegen] attempt {attempt} failed: {e}")
    raise RuntimeError(f"image generation failed after 3 attempts: {last_err}")


# ── Photo editor: artifact review + prompt revision ─────────────────────────

REVIEW_PROMPT = """You are a strict photo editor for an e-commerce website. \
Inspect this AI-generated image for defects that would make a shopper \
distrust the page. Reject the image if you find ANY of:
- garbled or pseudo-text, illegible lettering, fake logos or watermarks
- anatomical errors (hands, fingers, faces, eyes, teeth, limbs)
- floating holograms / AR overlays / impossible screens or reflections
- warped geometry (furniture, products, screens, buildings)
- uncanny or distorted faces, duplicated people, merged bodies
- obvious rendering artifacts (smearing, ghosting, seams, noise patches)
Small out-of-focus background text on real screens/signage is acceptable if \
illegible by design and not garbled gibberish in focus.
Reply with ONLY a JSON object: {"suitable": true|false, \
"issues": ["<short description of each defect found>"]}"""

REVISE_PROMPT = """An image-generation prompt keeps producing images that a \
photo editor rejects. Rewrite the prompt to avoid the recurring defects while \
keeping the same subject and commercial setting. Keep it under 90 \
words, photographic and concrete; explicitly steer away from the failure \
modes listed. Reply with ONLY the revised prompt text.

Original prompt: {prompt}

Recurring defects: {issues}"""


def _review_image(client: genai.Client, path: Path) -> tuple[bool, list[str]]:
    """Ask the review model to comb the image for AI artifacts."""
    blob = genai_types.Part.from_bytes(
        data=path.read_bytes(), mime_type="image/png")
    resp = client.models.generate_content(
        model=REVIEW_MODEL, contents=[blob, REVIEW_PROMPT])
    text = (resp.text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S).strip()
    try:
        verdict = json.loads(text)
        return bool(verdict.get("suitable")), [str(i) for i in
                                               (verdict.get("issues") or [])]
    except (json.JSONDecodeError, AttributeError):
        # Unreadable verdict: fail safe — treat as unsuitable with a note.
        return False, [f"reviewer returned unparseable verdict: {text[:120]}"]


def _revise_prompt(client: genai.Client, prompt: str, issues: list[str]) -> str:
    resp = client.models.generate_content(
        model=REVIEW_MODEL,
        contents=REVISE_PROMPT.format(prompt=prompt, issues="; ".join(issues)))
    revised = (resp.text or "").strip().strip('"')
    return revised or prompt


def _generate_reviewed(client: genai.Client, prompt: str,
                       out_stem: Path) -> tuple[Path, str, list[str]]:
    """Generate -> review loop. Returns (path, final_prompt, review_log).

    Up to ATTEMPTS_PER_PROMPT tries per prompt; after that many consecutive
    rejections the prompt is revised from the reviewer's notes (max
    MAX_PROMPTS prompts). Raises if nothing passes."""
    log: list[str] = []
    current = prompt
    collected: list[str] = []
    for round_no in range(1, MAX_PROMPTS + 1):
        collected = []
        for attempt in range(1, ATTEMPTS_PER_PROMPT + 1):
            path = _generate_one(client, current, out_stem)
            ok, issues = _review_image(client, path)
            if ok:
                log.append(f"prompt {round_no} attempt {attempt}: accepted")
                return path, current, log
            collected.extend(issues)
            log.append(f"prompt {round_no} attempt {attempt}: rejected "
                       f"({'; '.join(issues) or 'no reason given'})")
            print(f"[imagegen]   editor rejected attempt {attempt}: "
                  f"{'; '.join(issues)[:140]}")
        if round_no < MAX_PROMPTS:
            current = _revise_prompt(client, current, collected)
            log.append(f"prompt revised -> {current[:100]}")
            print(f"[imagegen]   3 consecutive rejections; revised prompt: "
                  f"{current[:100]}...")
    raise RuntimeError(
        f"photo editor rejected all {MAX_PROMPTS * ATTEMPTS_PER_PROMPT} "
        f"attempts across {MAX_PROMPTS} prompts; last issues: "
        f"{'; '.join(collected[-3:])}")


def main() -> int:
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else common.POST_OUT
    if not src.exists():
        print(f"[imagegen] ERROR: {src.name} not found. Run generator.py first.")
        return 1
    post = json.loads(src.read_text(encoding="utf-8"))
    prompts = post.get("image_prompts") or []
    if not prompts:
        print("[imagegen] ERROR: post has no image_prompts.")
        return 1

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("[imagegen] ERROR: GEMINI_API_KEY not set (env or .env).")
        return 1
    client = genai.Client(api_key=api_key)

    body = post.get("body_html", "")
    # Alt text: prefer the image_prompts alt (the hero is no longer in the body),
    # falling back to any alt on the in-body <img> tag.
    alts = {}
    for ip in prompts:
        pl = (ip.get("placement") or "").lower().replace("_", "-")
        token = common.HERO_IMG if pl == "hero" else common.INBODY_IMG
        alts[pl] = (ip.get("alt") or "").strip() or _alt_for(body, token)
    handle = post.get("handle", "post")

    OUT_DIR.mkdir(exist_ok=True)
    manifest = {}
    for ip in prompts:
        placement = (ip.get("placement") or "").lower().replace("_", "-")
        prompt = (ip.get("prompt") or "").strip()
        if placement not in ("hero", "in-body") or not prompt:
            print(f"[imagegen] skipping malformed entry: {ip}")
            continue
        print(f"[imagegen] Generating {placement} image with {MODEL} "
              f"(reviewed by {REVIEW_MODEL})...")
        path, final_prompt, review_log = _generate_reviewed(
            client, prompt, OUT_DIR / f"{handle}-{placement}")
        manifest[placement] = {
            "file": path.name,
            "alt": alts.get(placement, ""),
            "prompt": final_prompt,
            "review_log": review_log,
        }
        print(f"[imagegen]   {path.name} ({path.stat().st_size // 1024} KB)  "
              f"alt: {manifest[placement]['alt'][:60]}")

    (OUT_DIR / "manifest.json").write_text(
        json.dumps({"handle": handle, "images": manifest}, indent=2,
                   ensure_ascii=False), encoding="utf-8")
    print(f"[imagegen] Wrote images/manifest.json "
          f"({len(manifest)}/{len(prompts)} images).")
    return 0 if len(manifest) == len(prompts) else 1


if __name__ == "__main__":
    sys.exit(main())
