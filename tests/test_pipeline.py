"""
test_pipeline.py — unit + negative tests for the pure, offline parts of the
pipeline. No network, no API keys. Modules that require the anthropic/requests
SDKs are guarded with importorskip so the suite runs on a fresh checkout.

Run:  python -m pytest
"""

import json
import sys
from pathlib import Path

import pytest

# Make the repo root importable when pytest is run from anywhere.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import common          # noqa: E402  (stdlib + config only)
import final_check     # noqa: E402  (stdlib only)


# ── common.slugify ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("Best USB-C Cables 2026", "best-usb-c-cables-2026"),
    ("  Leading/Trailing  ", "leading-trailing"),
    ("multiple   spaces & symbols!!!", "multiple-spaces-symbols"),
    ("already-a-slug", "already-a-slug"),
])
def test_slugify(raw, expected):
    assert common.slugify(raw) == expected


# ── common.trim_meta / tidy_meta ──────────────────────────────────────────────

def test_trim_meta_respects_limit():
    text = "Buy premium widgets online with fast shipping and a lifetime warranty today"
    out = common.trim_meta(text, common.META_TITLE_MAX)
    assert len(out) <= common.META_TITLE_MAX


def test_trim_meta_short_text_untouched_except_tidy():
    assert common.trim_meta("Short and clean", 60) == "Short and clean"


def test_tidy_meta_drops_dangling_stopword():
    assert common.tidy_meta("ensure compliance and") == "ensure compliance"
    assert common.tidy_meta("fast shipping,") == "fast shipping"


# ── common.space_blocks ───────────────────────────────────────────────────────

def test_space_blocks_separates_blocks():
    html = "<h2>Title</h2><p>One.</p><p>Two.</p>"
    out = common.space_blocks(html)
    assert "</h2>\n\n<p>" in out
    assert "\n\n\n" not in out  # never triple-newline


# ── common.select_internal_links dedupes and includes hubs ────────────────────

def test_select_internal_links_dedupes():
    links = [
        {"url": "/collections/usb-cables", "title": "USB Cables", "description": ""},
        {"url": "/collections/usb-cables", "title": "dup", "description": ""},
        {"url": "/pages/about-us", "title": "About", "description": ""},
    ]
    chosen = common.select_internal_links("usb cables", "", links, n=10)
    urls = [c["url"] for c in chosen]
    assert len(urls) == len(set(urls)), "no duplicate URLs"


# ── common.load_dotenv: shell env WINS over the .env file (trust boundary) ────

def test_load_dotenv_does_not_override_shell(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("ANTHROPIC_API_KEY=from-file\nGEMINI_API_KEY=file-gem\n",
                   encoding="utf-8")
    monkeypatch.setattr(common, "ENV_FILE", env)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "from-shell")  # already set
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)    # not set
    common.load_dotenv()
    assert common.os.environ["ANTHROPIC_API_KEY"] == "from-shell"  # shell wins
    assert common.os.environ["GEMINI_API_KEY"] == "file-gem"       # file fills the gap


# ── final_check.validate: blockers at the publish boundary ────────────────────

def _good_post():
    return {
        "title": "Best USB-C Cables",
        "handle": "best-usb-c-cables",
        "seo_title": "Best USB-C Cables (2026 Buyer's Guide)",
        "seo_description": "Compare the best USB-C cables for charging and data.",
        "summary_html": "<p>A quick guide to choosing USB-C cables.</p>",
        "tags": ["usb-c", "cables"],
        "body_html": "<h2>Intro</h2>\n\n<p>Body</p>\n\n"
                     "<img src='{{INBODY_IMAGE}}' alt='cable'>",
        "image_prompts": [{"placement": "hero", "prompt": "x",
                           "alt": "best usb-c cables hero"},
                          {"placement": "in-body", "prompt": "y", "alt": "cable"}],
        "keyword_used": "best usb-c cables",
    }


def _good_manifest():
    return {"images": {
        "hero": {"file": "h.png", "alt": "best usb-c cables"},
        "in-body": {"file": "b.png", "alt": "cable"},
    }}


def test_validate_flags_missing_placeholder():
    post = _good_post()
    post["body_html"] = post["body_html"].replace("{{INBODY_IMAGE}}", "")
    blockers, _ = final_check.validate(post, _good_manifest())
    assert any("in-body placeholder" in b for b in blockers)


def test_validate_flags_missing_required_field():
    post = _good_post()
    del post["handle"]
    blockers, _ = final_check.validate(post, _good_manifest())
    assert any("handle" in b for b in blockers)


def test_validate_flags_missing_image_file(tmp_path, monkeypatch):
    # Manifest references files that don't exist on disk -> blocker.
    monkeypatch.setattr(final_check, "HERE", tmp_path)
    blockers, _ = final_check.validate(_good_post(), _good_manifest())
    assert any("image file missing on disk" in b for b in blockers)


def test_validate_warns_on_long_meta():
    post = _good_post()
    post["seo_description"] = "x" * 200
    _, warnings = final_check.validate(post, _good_manifest())
    assert any("seo_description" in w for w in warnings)


# ── editor: deterministic checks (needs anthropic import at module load) ──────

def test_editor_code_checks():
    pytest.importorskip("anthropic")
    import editor

    # British spelling conversion.
    fixed, n = editor._apply_british_spelling("We optimize and analyze color.")
    assert n == 3 and "optimise" in fixed and "colour" in fixed

    # FAQ counting inside the FAQ section only.
    body = ("<h2>Frequently asked questions</h2>\n"
            "<h3>Q1?</h3><p>A1</p><h3>Q2?</h3><p>A2</p>")
    assert editor._faq_count(body) == 2

    # run_code_checks blocks a thin body missing placeholders + with an <h1>.
    thin = {"body_html": "<h1>Nope</h1><p>tiny</p>", "keyword_used": "widgets"}
    result = editor.run_code_checks(thin)
    joined = " ".join(result["blockers"])
    assert "too thin" in joined
    assert "in-body image placeholder" in joined
    assert "<h1>" in joined


# ── publisher: draft-only + SEO metafields (needs requests) ───────────────────

def test_publisher_builds_draft_and_metafields():
    pytest.importorskip("requests")
    import publisher

    post = _good_post()
    article = publisher.build_article_input(
        post, "gid://shopify/Blog/1", post["body_html"],
        "https://cdn/x.png", "alt text")
    assert article["isPublished"] is False, "must create as DRAFT"
    assert article["blogId"] == "gid://shopify/Blog/1"
    assert article["image"]["url"] == "https://cdn/x.png"

    mf = publisher.seo_metafields("gid://shopify/Article/9", post)
    keys = {m["key"] for m in mf}
    assert keys == {"title_tag", "description_tag"}
    assert all(m["namespace"] == "global" for m in mf)


def test_generator_ingest_validates_and_writes(tmp_path, monkeypatch):
    """Claude Code mode: do_ingest reads the article JSON, validates/normalises,
    and writes post.json (handle slugified, keyword_used set)."""
    pytest.importorskip("anthropic")
    import generator

    (tmp_path / "keyword.json").write_text(
        json.dumps({"keyword": "best usb-c cables"}), encoding="utf-8")
    article = _good_post()
    del article["keyword_used"]          # ingest fills this from keyword.json
    article["handle"] = "Best USB-C Cables"  # must be slugified on ingest
    resp = tmp_path / "generation_response.json"
    resp.write_text(json.dumps(article), encoding="utf-8")

    out = tmp_path / "post.json"
    monkeypatch.setattr(generator, "RESPONSE_FILE", resp)
    monkeypatch.setattr(generator, "KEYWORD_IN", tmp_path / "keyword.json")
    monkeypatch.setattr(generator, "POST_OUT", out)

    assert generator.do_ingest() == 0
    written = json.loads(out.read_text(encoding="utf-8"))
    assert written["handle"] == "best-usb-c-cables"
    assert written["keyword_used"] == "best usb-c cables"
    assert "generated_at" in written


def test_generator_ingest_rejects_missing_placeholder(tmp_path, monkeypatch):
    pytest.importorskip("anthropic")
    import generator
    (tmp_path / "keyword.json").write_text(json.dumps({"keyword": "x"}), encoding="utf-8")
    article = _good_post()
    article["body_html"] = article["body_html"].replace("{{INBODY_IMAGE}}", "")
    resp = tmp_path / "generation_response.json"
    resp.write_text(json.dumps(article), encoding="utf-8")
    monkeypatch.setattr(generator, "RESPONSE_FILE", resp)
    monkeypatch.setattr(generator, "KEYWORD_IN", tmp_path / "keyword.json")
    assert generator.do_ingest() == 1   # missing in-body placeholder -> refuse


def test_seo_check_scores_and_blocks():
    import seo_check

    # A well-formed article for keyword "widget stands" scores well and has a
    # valid FAQPage schema mirroring the FAQ.
    faq_ld = json.dumps({"@context": "https://schema.org", "@type": "FAQPage",
                         "mainEntity": [{"@type": "Question", "name": f"Q{i}?",
                                         "acceptedAnswer": {"@type": "Answer", "text": "A"}}
                                        for i in range(4)]})
    good = {
        "keyword_used": "widget stands",
        "title": "Widget Stands: A Buyer's Guide",
        "seo_title": "Widget Stands: A Buyer's Guide",
        "seo_description": "Choosing widget stands? Compare types and sizing in this guide.",
        "handle": "widget-stands",
        "summary_html": "<p>Widget stands explained.</p>",
        "image_prompts": [{"placement": "hero", "prompt": "x", "alt": "widget stands on a desk"}],
        "body_html": (
            "<p>Widget stands are supports for widgets. This guide covers widget "
            "stands for offices, sizing and setup so you can choose widget stands "
            "with confidence.</p>\n"
            "<p>Browse our <a href='/collections/widget-stands'>widget stands</a>, "
            "the <a href='/collections/all'>full range</a>, our "
            "<a href='/pages/about'>about page</a> and "
            "<a href='/pages/contact'>contact page</a>.</p>\n<ul><li>point</li></ul>\n"
            "<table><thead><tr><th>A</th></tr></thead><tbody><tr><td>1</td></tr></tbody></table>\n"
            "<img src='{{INBODY_IMAGE}}' alt='a widget stand'>\n"
            "<h2>Why use widget stands?</h2><p>They lift widgets to eye level.</p>\n"
            "<h2>How to choose widget stands</h2><p>Match the size to your widget.</p>\n"
            "<h2>Frequently asked questions</h2>\n"
            "<h3>Are widget stands adjustable?</h3><p>Most are height adjustable.</p>\n"
            "<h3>Do widget stands fit all widgets?</h3><p>Check the mounting size first.</p>\n"
            "<h3>Are widget stands worth it?</h3><p>Yes, for comfort and airflow.</p>\n"
            "<h3>How much do widget stands cost?</h3><p>They are an affordable one-off buy.</p>\n"
            # Keyword-free filler: keeps density natural (~1%) and clears the word floor.
            + "<p>" + ("This section adds practical buying detail for a reader to consider. " * 130) + "</p>\n"
            + f"<script type=\"application/ld+json\">{faq_ld}</script>"),
    }
    r = seo_check.score(good)
    assert r["aeo_score"] == 100, r["aeo"]
    assert r["seo_score"] >= 97, r["seo"]           # natural density, not stuffed
    assert not r["blockers"], r["blockers"]
    kwd = next(c for c in r["seo"] if c["label"].startswith("Keyword density"))
    assert kwd["status"] == "pass", kwd             # ~1% density passes

    # Over-stuffing the exact phrase should WARN on density (not pass).
    stuffed = dict(good)
    stuffed["body_html"] = good["body_html"].replace(
        "This section adds practical buying detail for a reader to consider. ",
        "widget stands widget stands widget stands ")
    rs = seo_check.score(stuffed)
    kwd_s = next(c for c in rs["seo"] if c["label"].startswith("Keyword density"))
    assert kwd_s["status"] == "warn", kwd_s

    # Missing keyword in SEO title + invalid FAQ schema -> blockers.
    bad = dict(good)
    bad["seo_title"] = "A Generic Title With No Focus Term"
    bad["body_html"] = good["body_html"].replace(faq_ld, "{ not valid json")
    rb = seo_check.score(bad)
    labels = " ".join(rb["blockers"])
    assert "SEO title" in labels
    assert "FAQPage" in labels


def test_publisher_raises_on_user_errors():
    pytest.importorskip("requests")
    import publisher
    with pytest.raises(publisher.ShopifyError):
        publisher.ShopifyClient.raise_user_errors(
            {"userErrors": [{"field": "handle", "message": "taken"}]}, "articleCreate")
    # No errors -> no raise.
    publisher.ShopifyClient.raise_user_errors({"userErrors": []}, "articleCreate")


# ── shopify_oauth: code extraction from the pasted redirect (needs requests) ──

def test_oauth_extract_code():
    pytest.importorskip("requests")
    import shopify_oauth
    code, state = shopify_oauth.extract_code(
        "https://localhost/callback?code=abc123&hmac=x&state=s9&shop=z.myshopify.com")
    assert code == "abc123" and state == "s9"
    # Bare code (no URL) -> code returned, state unknown.
    code2, state2 = shopify_oauth.extract_code("  rawcode456  ")
    assert code2 == "rawcode456" and state2 is None


def test_oauth_authorize_url_shape():
    pytest.importorskip("requests")
    import shopify_oauth
    url = shopify_oauth.build_authorize_url(
        "s.myshopify.com", "clientid", "https://localhost/callback", "nonce1")
    assert url.startswith("https://s.myshopify.com/admin/oauth/authorize?")
    assert "client_id=clientid" in url
    assert "scope=write_content" in url  # scopes present, urlencoded
    assert "state=nonce1" in url


# ── scraper: the SerpReport project is SHARED with the WordPress site ──────────
# The project tracks shop.winpro.com.sg AND winpro.com.sg; rows are told apart
# only by the "URL Found" path. Without the site filter the picker can hand a
# WordPress services keyword to this storefront pipeline. Negative tests first.

def _scraper():
    pytest.importorskip("playwright")
    import scraper
    return scraper


def test_is_own_site_rejects_wordpress_paths():
    scraper = _scraper()
    for path in ("/it-managed-services/",
                 "/vulnerability-assessment-penetration-testing-vapt/",
                 "/it-helpdesk/help-desk-it-support-in-singapore/",
                 "/welcome-to-microsoft-365/",
                 "/wp-content/uploads/2022/12/x.png"):
        assert scraper.is_own_site(path) is False, path


def test_is_own_site_denies_unattributable_paths():
    # Deny-by-default: no URL means we cannot prove the keyword ranks on OUR
    # site, so it must not be publishable here.
    scraper = _scraper()
    for path in ("", "   ", None):
        assert scraper.is_own_site(path) is False


def test_is_own_site_accepts_storefront_paths():
    scraper = _scraper()
    for path in ("/collections/all",
                 "/products/thinkpad-x1",
                 "/blogs/news/lenovo-tablet-series-overview",
                 "/pages/locate-us"):
        assert scraper.is_own_site(path) is True, path


def test_pick_keyword_never_returns_a_wordpress_keyword(monkeypatch):
    scraper = _scraper()
    monkeypatch.setattr(scraper, "load_posted", lambda: set())
    rows = [
        # Far higher volume, but it ranks on the WordPress services site.
        {"keyword": "microsoft 365", "position": 11, "change": 0, "local_vol": 40500,
         "url_found": "/welcome-to-microsoft-365/"},
        # Lower volume, but genuinely ours.
        {"keyword": "lenovo tablet", "position": 8, "change": -3, "local_vol": 1600,
         "url_found": "/blogs/news/lenovo-tablet-series-overview"},
    ]
    best = scraper.pick_keyword(rows)
    assert best is not None
    assert best["keyword"] == "lenovo tablet"
    assert scraper.is_own_site(best["url_found"])


def test_pick_keyword_returns_none_when_only_wordpress_rows_qualify():
    scraper = _scraper()
    rows = [
        {"keyword": "microsoft 365", "position": 11, "change": 0, "local_vol": 40500,
         "url_found": "/welcome-to-microsoft-365/"},
        {"keyword": "it helpdesk", "position": 8, "change": 0, "local_vol": 320,
         "url_found": "/it-helpdesk-services/"},
    ]
    assert scraper.pick_keyword(rows) is None
