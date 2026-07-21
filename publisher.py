"""
publisher.py
------------
Step 4 of the pipeline: publish post.json (+ images/manifest.json) to Shopify
as an UNPUBLISHED (draft) blog article, via the GraphQL Admin API.

Human-in-the-loop safety property (preserved from the WordPress pipeline): the
article is created as a DRAFT (isPublished: false). A person reviews and
publishes it in the Shopify admin. This script never publishes live.

Flow (only with --publish):
  1. Resolve the target blog GID from config.SHOPIFY["blog_handle"].
  2. For each image in images/manifest.json: stagedUploadsCreate -> PUT bytes ->
     fileCreate -> poll until READY -> collect the CDN URL.
  3. Swap {{HERO_IMAGE}} / {{INBODY_IMAGE}} in body_html for the real URLs;
     set the hero as the article's image.
  4. articleCreate (draft) with body, handle, summary, tags, author, image.
  5. Set SEO metafields: namespace "global", keys "title_tag" / "description_tag".

Modes:
  python publisher.py --check      # verify connectivity + token (shop query)
  python publisher.py              # DRY RUN: read-only; print the plan, no writes
  python publisher.py --publish    # actually create the draft article + upload

Secrets (env / .env):  SHOPIFY_STORE=xxx.myshopify.com   SHOPIFY_ADMIN_TOKEN=shpat_...
API version is pinned in config.SHOPIFY["api_version"].

!!! VERIFY BEFORE FIRST REAL PUBLISH !!!
Shopify deprecates the Admin API quarterly and field/mutation names DO change.
Confirm every name used below against the pinned version's schema at
https://shopify.dev/docs/api/admin-graphql/<version>/ — do not trust these from
memory. The names most likely to drift: ArticleCreateInput.body vs bodyHtml,
the author field shape, whether articleCreate takes an `seo` input, and the
fileCreate/MediaImage status field. See docs/PUBLISHING.md.
"""

import json
import os
import sys
import time
from pathlib import Path

import requests

import common  # noqa: F401  (loads .env at import)
import config as cfg

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

HERE = Path(__file__).parent
POST_IN = HERE / "post.json"
MANIFEST_IN = HERE / "images" / "manifest.json"

API_VERSION = cfg.SHOPIFY["api_version"]
BLOG_HANDLE = cfg.SHOPIFY["blog_handle"]


class ShopifyError(RuntimeError):
    pass


class ShopifyClient:
    """Thin GraphQL Admin API client with basic cost-based throttle handling."""

    def __init__(self, store: str, token: str, api_version: str):
        self.endpoint = f"https://{store}/admin/api/{api_version}/graphql.json"
        self.headers = {
            "X-Shopify-Access-Token": token,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def gql(self, query: str, variables: dict | None = None) -> dict:
        """Run a GraphQL operation; raise on transport, GraphQL, or userErrors."""
        for attempt in range(1, 6):
            resp = requests.post(
                self.endpoint, headers=self.headers,
                json={"query": query, "variables": variables or {}}, timeout=60)
            if resp.status_code == 429:  # REST-style rate limit (rare on GraphQL)
                time.sleep(2 * attempt)
                continue
            if resp.status_code >= 400:
                raise ShopifyError(f"HTTP {resp.status_code}: {resp.text[:300]}")
            payload = resp.json()
            if "errors" in payload:
                # THROTTLED is cost-based; back off and retry.
                if any(e.get("extensions", {}).get("code") == "THROTTLED"
                       for e in payload["errors"]):
                    time.sleep(2 * attempt)
                    continue
                raise ShopifyError(f"GraphQL errors: {payload['errors']}")
            return payload["data"]
        raise ShopifyError("gave up after repeated throttling")

    @staticmethod
    def raise_user_errors(node: dict, where: str) -> None:
        errs = node.get("userErrors") or []
        if errs:
            raise ShopifyError(f"{where} userErrors: {errs}")


# ── Queries / mutations (VERIFY names against the pinned API version) ────────

Q_SHOP = "{ shop { name myshopifyDomain } }"

Q_BLOGS = """
query Blogs { blogs(first: 50) { edges { node { id handle title } } } }
"""

M_STAGED_UPLOAD = """
mutation StagedUpload($input: [StagedUploadInput!]!) {
  stagedUploadsCreate(input: $input) {
    stagedTargets { url resourceUrl parameters { name value } }
    userErrors { field message }
  }
}
"""

M_FILE_CREATE = """
mutation FileCreate($files: [FileCreateInput!]!) {
  fileCreate(files: $files) {
    files { id fileStatus alt
      ... on MediaImage { image { url } } }
    userErrors { field message }
  }
}
"""

Q_FILE_STATUS = """
query FileStatus($id: ID!) {
  node(id: $id) {
    ... on MediaImage { id fileStatus image { url } }
  }
}
"""

# NOTE: ArticleCreateInput / articleCreate confirmed present from API 2024-10+.
# Verify field names (body vs bodyHtml, author, image, isPublished) for YOUR
# pinned version before --publish.
M_ARTICLE_CREATE = """
mutation ArticleCreate($article: ArticleCreateInput!) {
  articleCreate(article: $article) {
    article { id title handle isPublished }
    userErrors { field message }
  }
}
"""

M_ARTICLE_UPDATE = """
mutation ArticleUpdate($id: ID!, $article: ArticleUpdateInput!) {
  articleUpdate(id: $id, article: $article) {
    article { id title handle isPublished }
    userErrors { field message }
  }
}
"""

M_METAFIELDS_SET = """
mutation MetafieldsSet($metafields: [MetafieldsSetInput!]!) {
  metafieldsSet(metafields: $metafields) {
    metafields { id namespace key }
    userErrors { field message }
  }
}
"""


# ── Steps ────────────────────────────────────────────────────────────────────

def resolve_blog_gid(client: ShopifyClient, handle: str) -> str:
    data = client.gql(Q_BLOGS)
    for edge in data["blogs"]["edges"]:
        if edge["node"]["handle"] == handle:
            return edge["node"]["id"]
    available = [e["node"]["handle"] for e in data["blogs"]["edges"]]
    raise ShopifyError(f"blog handle '{handle}' not found. Available: {available}")


def upload_image(client: ShopifyClient, path: Path, alt: str) -> str:
    """stagedUploadsCreate -> PUT/POST the bytes -> fileCreate -> poll READY.
    Returns the CDN image URL."""
    mime = "image/png" if path.suffix == ".png" else "image/jpeg"
    staged = client.gql(M_STAGED_UPLOAD, {"input": [{
        "filename": path.name, "mimeType": mime,
        "resource": "IMAGE", "httpMethod": "POST",
    }]})["stagedUploadsCreate"]
    ShopifyClient.raise_user_errors(staged, "stagedUploadsCreate")
    target = staged["stagedTargets"][0]

    # POST the bytes to the staged target (form fields first, then the file).
    form = {p["name"]: p["value"] for p in target["parameters"]}
    with path.open("rb") as fh:
        up = requests.post(target["url"], data=form,
                           files={"file": (path.name, fh, mime)}, timeout=120)
    if up.status_code not in (200, 201, 204):
        raise ShopifyError(f"staged upload PUT failed: HTTP {up.status_code} {up.text[:200]}")

    created = client.gql(M_FILE_CREATE, {"files": [{
        "originalSource": target["resourceUrl"],
        "contentType": "IMAGE", "alt": alt,
    }]})["fileCreate"]
    ShopifyClient.raise_user_errors(created, "fileCreate")
    file_id = created["files"][0]["id"]

    # fileCreate is async; poll until READY to get the CDN URL.
    for _ in range(20):
        node = client.gql(Q_FILE_STATUS, {"id": file_id})["node"] or {}
        if node.get("fileStatus") == "READY" and node.get("image", {}).get("url"):
            return node["image"]["url"]
        if node.get("fileStatus") == "FAILED":
            raise ShopifyError(f"file processing FAILED for {path.name}")
        time.sleep(3)
    raise ShopifyError(f"image {path.name} not READY after polling")


def build_article_input(post: dict, blog_gid: str, body_html: str,
                        hero_url: str | None, hero_alt: str) -> dict:
    article = {
        "blogId": blog_gid,
        "title": post["title"],
        "handle": post["handle"],
        "body": body_html,                 # VERIFY: body vs bodyHtml for your version
        "summary": post.get("summary_html", ""),
        "tags": post.get("tags", []),
        "author": {"name": cfg.SITE.get("author") or cfg.SITE["short_name"]},
        "isPublished": False,              # DRAFT — human publishes in admin
    }
    if hero_url:
        article["image"] = {"url": hero_url, "altText": hero_alt}
    return article


def seo_metafields(article_gid: str, post: dict) -> list[dict]:
    return [
        {"ownerId": article_gid, "namespace": "global", "key": "title_tag",
         "type": "single_line_text_field", "value": post.get("seo_title", "")},
        {"ownerId": article_gid, "namespace": "global", "key": "description_tag",
         "type": "single_line_text_field", "value": post.get("seo_description", "")},
    ]


# ── Entry point ───────────────────────────────────────────────────────────────

def _load(path: Path):
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, ValueError):
        return None


def main() -> int:
    argv = sys.argv[1:]
    args = set(argv)
    # --update <article_gid> updates an existing draft instead of creating one.
    update_gid = None
    if "--update" in argv:
        i = argv.index("--update")
        if i + 1 < len(argv):
            update_gid = argv[i + 1]
        else:
            print("[publisher] ERROR: --update needs an article GID "
                  "(e.g. --update gid://shopify/Article/123).")
            return 1
    store = os.environ.get("SHOPIFY_STORE", "")
    token = os.environ.get("SHOPIFY_ADMIN_TOKEN", "")
    if not store or not token:
        print("[publisher] ERROR: set SHOPIFY_STORE and SHOPIFY_ADMIN_TOKEN in .env.")
        return 1
    client = ShopifyClient(store, token, API_VERSION)

    if "--check" in args:
        try:
            shop = client.gql(Q_SHOP)["shop"]
            print(f"[publisher] OK: connected to '{shop['name']}' "
                  f"({shop['myshopifyDomain']}), API {API_VERSION}.")
            blog_gid = resolve_blog_gid(client, BLOG_HANDLE)
            print(f"[publisher] Target blog '{BLOG_HANDLE}' -> {blog_gid}")
            return 0
        except ShopifyError as e:
            print(f"[publisher] ERROR: {e}")
            return 1

    post = _load(POST_IN)
    if post is None:
        print("[publisher] ERROR: post.json not found/unreadable. Run generator + editor.")
        return 1
    manifest = _load(MANIFEST_IN) or {"images": {}}
    imgs = manifest.get("images", {})
    hero = imgs.get("hero", {})
    inbody = imgs.get("in-body", {})

    publish = "--publish" in args or update_gid is not None
    if not publish:
        print("[publisher] DRY RUN (read-only). Re-run with --publish to create the draft.\n")
        blog_gid = "<resolved-at-publish>"
        try:
            blog_gid = resolve_blog_gid(client, BLOG_HANDLE)
        except ShopifyError as e:
            print(f"[publisher] (could not resolve blog now: {e})")
        article = build_article_input(post, blog_gid, post["body_html"],
                                      None, hero.get("alt", ""))
        print("[publisher] PLANNED articleCreate input (images still as placeholders):")
        print(json.dumps({k: (v if k != "body" else f"<{len(v)} chars HTML>")
                          for k, v in article.items()}, indent=2, ensure_ascii=False))
        print(f"\n[publisher] Images to upload on --publish: "
              f"hero={hero.get('file')!r}, in-body={inbody.get('file')!r}")
        print("[publisher] SEO metafields: global/title_tag, global/description_tag")
        return 0

    # ── Real publish ──
    try:
        blog_gid = resolve_blog_gid(client, BLOG_HANDLE)
        print(f"[publisher] Target blog -> {blog_gid}")

        body_html = post["body_html"]
        hero_url = None
        for placement, entry, token_str in (
            ("hero", hero, common.HERO_IMG),
            ("in-body", inbody, common.INBODY_IMG),
        ):
            fname = entry.get("file")
            if not fname:
                print(f"[publisher] WARNING: no {placement} image in manifest; "
                      f"leaving {token_str} in body.")
                continue
            fpath = HERE / "images" / fname
            if not fpath.exists():
                raise ShopifyError(f"{placement} image file missing: {fpath}")
            print(f"[publisher] Uploading {placement} image {fname}...")
            url = upload_image(client, fpath, entry.get("alt", ""))
            body_html = body_html.replace(token_str, url)
            if placement == "hero":
                hero_url = url
            print(f"[publisher]   -> {url}")

        article_input = build_article_input(
            post, blog_gid, body_html, hero_url, hero.get("alt", ""))
        if update_gid:
            # ArticleUpdateInput doesn't take blogId unless moving blogs.
            upd = {k: v for k, v in article_input.items() if k != "blogId"}
            result = client.gql(M_ARTICLE_UPDATE,
                                {"id": update_gid, "article": upd})["articleUpdate"]
            ShopifyClient.raise_user_errors(result, "articleUpdate")
            art = result["article"]
            print(f"[publisher] Updated DRAFT article: {art['id']} "
                  f"(handle '{art['handle']}', isPublished={art['isPublished']}).")
        else:
            result = client.gql(M_ARTICLE_CREATE,
                                {"article": article_input})["articleCreate"]
            ShopifyClient.raise_user_errors(result, "articleCreate")
            art = result["article"]
            print(f"[publisher] Created DRAFT article: {art['id']} "
                  f"(handle '{art['handle']}', isPublished={art['isPublished']}).")

        mf = client.gql(M_METAFIELDS_SET,
                        {"metafields": seo_metafields(art["id"], post)})["metafieldsSet"]
        ShopifyClient.raise_user_errors(mf, "metafieldsSet")
        print(f"[publisher] Set {len(mf['metafields'])} SEO metafield(s).")

        print("\n[publisher] DONE. Review and publish the draft in the Shopify admin, "
              "then run:  python final_check.py --mark-posted")
        return 0
    except ShopifyError as e:
        print(f"[publisher] ERROR: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
