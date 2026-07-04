#!/usr/bin/env python3
"""Render the Joice blog from posts/*.md.

Outputs (all committed to the repo; Cloudflare Pages serves them as-is):
  blog/index.html          timeline + search listing
  blog/<slug>/index.html   one page per post
  blog/posts.json          machine-readable index (search + LLM findability)
  blog/feed.xml            RSS 2.0
  sitemap.xml              whole-site sitemap
  robots.txt               crawl policy + sitemap pointer
  llms.txt                 LLM-facing site map (llmstxt.org convention)

Deterministic: same inputs -> byte-identical outputs, so CI can check that
committed HTML is in sync with the markdown sources.
"""

import html
import json
import re
import urllib.parse
from email.utils import format_datetime
from datetime import datetime, timezone
from pathlib import Path

import markdown
import yaml

ROOT = Path(__file__).resolve().parent.parent
POSTS_DIR = ROOT / "posts"
BLOG_DIR = ROOT / "blog"
PIPELINE_DIR = Path(__file__).resolve().parent

WORDS_PER_MINUTE = 220


def load_config():
    with open(PIPELINE_DIR / "config.yml") as f:
        return yaml.safe_load(f)


def esc(s):
    return html.escape(str(s), quote=True)


def parse_post(path):
    text = path.read_text()
    match = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.DOTALL)
    if not match:
        raise ValueError("{}: missing frontmatter".format(path))
    meta = yaml.safe_load(match.group(1))
    body_md = match.group(2).strip()
    for field in ("title", "slug", "date", "description"):
        if field not in meta:
            raise ValueError("{}: frontmatter missing '{}'".format(path, field))
    meta["date"] = str(meta["date"])
    meta["tags"] = meta.get("tags", [])
    meta["sources"] = meta.get("sources", [])
    words = len(re.findall(r"\w+", body_md))
    meta["word_count"] = words
    meta["reading_minutes"] = max(1, round(words / WORDS_PER_MINUTE))
    meta["body_md"] = body_md
    meta["body_html"] = markdown.markdown(body_md, extensions=["extra"])
    return meta


def pretty_date(iso):
    d = datetime.strptime(iso, "%Y-%m-%d")
    return d.strftime("%B %-d, %Y").lower()


def month_label(iso):
    return datetime.strptime(iso, "%Y-%m-%d").strftime("%B %Y").lower()


def nav_html(cfg):
    return """    <nav>
        <div class="nav-inner">
            <a href="/" class="logo"><img src="/assets/joice-icon.png" alt="">joice</a>
            <div class="nav-links">
                <a href="/#features">features</a>
                <a href="/blog/">blog</a>
                <a href="/#download" class="btn-nav">download</a>
            </div>
        </div>
    </nav>"""


def footer_html(cfg):
    return """    <footer>
        <div class="footer-inner">
            <div class="footer-top">
                <div class="footer-brand">
                    <a href="/" class="logo"><img src="/assets/joice-icon.png" alt="">joice</a>
                    <p>A voice journal under a soft sky. Talk it out — Joice listens.</p>
                </div>
                <div class="footer-links">
                    <div class="footer-col">
                        <h4>product</h4>
                        <a href="/#features">features</a>
                        <a href="/blog/">blog</a>
                        <a href="/#download">download</a>
                    </div>
                    <div class="footer-col">
                        <h4>legal</h4>
                        <a href="/privacy.html">privacy policy</a>
                        <a href="/terms.html">terms of service</a>
                    </div>
                    <div class="footer-col">
                        <h4>support</h4>
                        <a href="/support.html">support center</a>
                        <a href="mailto:support@joiceapp.com">contact us</a>
                    </div>
                </div>
            </div>
            <div class="footer-bottom">
                <p>&copy; 2026 Joice. All rights reserved.</p>
                <p>Made with care, one conversation at a time.</p>
            </div>
        </div>
    </footer>"""


def head_html(cfg, title, description, canonical, extra=""):
    return """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{title}</title>
    <meta name="description" content="{description}">
    <link rel="canonical" href="{canonical}">
    <meta property="og:title" content="{title}">
    <meta property="og:description" content="{description}">
    <meta property="og:url" content="{canonical}">
    <meta property="og:site_name" content="Joice">
    <meta property="og:image" content="{site}/assets/joice-icon.png">
    <meta name="twitter:card" content="summary">
    <link rel="alternate" type="application/rss+xml" title="{blog_title}" href="{site}/blog/feed.xml">
    <link rel="icon" type="image/png" href="/assets/favicon.png">
    <link rel="apple-touch-icon" href="/assets/apple-touch-icon.png">
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Lora:ital,wght@0,400;0,500;0,600;1,400;1,500&family=DM+Sans:ital,opsz,wght@0,9..40,300;0,9..40,400;0,9..40,500;0,9..40,600;0,9..40,700;1,9..40,400&display=swap" rel="stylesheet">
    <link rel="stylesheet" href="/style.css">
{extra}</head>""".format(title=esc(title), description=esc(description),
                          canonical=canonical, site=cfg["site_url"],
                          blog_title=esc(cfg["blog_title"]), extra=extra)


def post_json_ld(cfg, post):
    url = "{}/blog/{}/".format(cfg["site_url"], post["slug"])
    data = {
        "@context": "https://schema.org",
        "@type": "BlogPosting",
        "headline": post["title"],
        "description": post["description"],
        "datePublished": post["date"],
        "url": url,
        "keywords": ", ".join(post["tags"]),
        "wordCount": post["word_count"],
        "author": {"@type": "Organization", "name": "Joice", "url": cfg["site_url"]},
        "publisher": {"@type": "Organization", "name": "Joice", "url": cfg["site_url"],
                      "logo": {"@type": "ImageObject",
                               "url": cfg["site_url"] + "/assets/joice-icon.png"}},
        "mainEntityOfPage": url,
        "citation": [
            {"@type": "CreativeWork", "name": s["title"], "url": s["url"]}
            for s in post["sources"]
        ],
        "isPartOf": {"@type": "Blog", "name": cfg["blog_title"],
                     "url": cfg["site_url"] + "/blog/"},
    }
    return '    <script type="application/ld+json">\n{}\n    </script>\n'.format(
        json.dumps(data, indent=2, ensure_ascii=False))


def render_post_page(cfg, post):
    url = "{}/blog/{}/".format(cfg["site_url"], post["slug"])
    tags = "\n".join(
        '                    <a class="pill blog-tag" href="/blog/?tag={0}">{1}</a>'.format(
            urllib.parse.quote(t), esc(t)) for t in post["tags"])
    sources = "\n".join(
        '                    <li><a href="{}" rel="nofollow noopener">{}</a>'
        '<span class="blog-source-pub"> — {}</span></li>'.format(
            esc(s["url"]), esc(s["title"]), esc(s.get("publisher", "")))
        for s in post["sources"])
    sources_section = """
                <aside class="blog-sources">
                    <h2>sources</h2>
                    <ul>
{}
                    </ul>
                </aside>""".format(sources) if post["sources"] else ""

    return """{head}
<body class="blog-body">
{nav}

    <main class="blog-post-page">
        <article>
            <header class="blog-post-head">
                <a class="blog-back" href="/blog/">&larr; the joice journal</a>
                <div class="blog-post-meta">
                    <span>{date}</span>
                    <span class="blog-meta-dot">&middot;</span>
                    <span>{minutes} min read</span>
                </div>
                <h1>{title}</h1>
                <p class="blog-post-desc">{description}</p>
                <div class="blog-post-tags">
{tags}
                </div>
            </header>

            <div class="blog-post-body">
{body}
            </div>
{sources_section}

            <aside class="blog-app-cta">
                <img src="/assets/leaf-branch.png" alt="" class="blog-cta-leaf">
                <div>
                    <h2>talk it out with joice</h2>
                    <p>A voice journal that listens like a friend. Free to try on iOS.</p>
                </div>
                <a href="{app_url}" class="btn-primary">get the app</a>
            </aside>
        </article>
    </main>

{footer}
</body>
</html>
""".format(head=head_html(cfg, post["title"] + " — the joice journal",
                          post["description"], url, extra=post_json_ld(cfg, post)),
           nav=nav_html(cfg), footer=footer_html(cfg),
           date=pretty_date(post["date"]), minutes=post["reading_minutes"],
           title=esc(post["title"]), description=esc(post["description"]),
           tags=tags, body=post["body_html"], sources_section=sources_section,
           app_url=cfg["app_store_url"])


def render_index_page(cfg, posts):
    all_tags = sorted({t for p in posts for t in p["tags"]})
    chips = "\n".join(
        '                <button class="blog-chip" data-tag="{0}">{0}</button>'.format(esc(t))
        for t in all_tags)

    cards, last_month = [], None
    for p in posts:
        month = month_label(p["date"])
        if month != last_month:
            cards.append('                <div class="blog-month" data-month>{}</div>'.format(month))
            last_month = month
        cards.append("""                <a class="blog-card" href="/blog/{slug}/"
                   data-title="{title_attr}" data-desc="{desc_attr}" data-tags="{tags_attr}" data-date="{date}">
                    <div class="blog-card-meta">
                        <span>{date_pretty}</span>
                        <span class="blog-meta-dot">&middot;</span>
                        <span>{minutes} min read</span>
                    </div>
                    <h2>{title}</h2>
                    <p>{desc}</p>
                    <div class="blog-card-tags">{tag_pills}</div>
                </a>""".format(
            slug=esc(p["slug"]),
            title=esc(p["title"]), title_attr=esc(p["title"].lower()),
            desc=esc(p["description"]), desc_attr=esc(p["description"].lower()),
            tags_attr=esc("|".join(p["tags"]).lower()), date=p["date"],
            date_pretty=pretty_date(p["date"]), minutes=p["reading_minutes"],
            tag_pills="".join('<span class="pill blog-tag">{}</span>'.format(esc(t))
                              for t in p["tags"])))

    empty = ("" if posts else
             '                <p class="blog-empty">first essays are on their way.</p>')

    search_js = """    <script>
        const input = document.getElementById('blog-search');
        const cards = Array.from(document.querySelectorAll('.blog-card'));
        const months = Array.from(document.querySelectorAll('[data-month]'));
        const chips = Array.from(document.querySelectorAll('.blog-chip'));
        const empty = document.getElementById('blog-no-results');
        let activeTag = null;

        function apply() {
            const q = input.value.trim().toLowerCase();
            let shown = 0;
            cards.forEach(card => {
                const hay = card.dataset.title + ' ' + card.dataset.desc + ' ' +
                            card.dataset.tags.split('|').join(' ') + ' ' + card.dataset.date;
                const matchesText = !q || hay.includes(q);
                const matchesTag = !activeTag || card.dataset.tags.split('|').includes(activeTag);
                const show = matchesText && matchesTag;
                card.style.display = show ? '' : 'none';
                if (show) shown++;
            });
            months.forEach(m => {
                let node = m.nextElementSibling, visible = false;
                while (node && !node.hasAttribute('data-month')) {
                    if (node.classList.contains('blog-card') && node.style.display !== 'none') visible = true;
                    node = node.nextElementSibling;
                }
                m.style.display = visible ? '' : 'none';
            });
            if (empty) empty.style.display = shown ? 'none' : '';
        }

        input.addEventListener('input', apply);
        chips.forEach(chip => chip.addEventListener('click', () => {
            activeTag = activeTag === chip.dataset.tag ? null : chip.dataset.tag;
            chips.forEach(c => c.classList.toggle('active', c.dataset.tag === activeTag));
            apply();
        }));

        const urlTag = new URLSearchParams(location.search).get('tag');
        if (urlTag) {
            const chip = chips.find(c => c.dataset.tag === urlTag);
            if (chip) chip.click();
        }
    </script>"""

    return """{head}
<body class="blog-body">
{nav}

    <header class="blog-hero">
        <div class="section-inner">
            <span class="eyebrow">essays from joice</span>
            <h1>{blog_title}</h1>
            <p class="blog-hero-sub">{blog_desc}</p>
            <div class="blog-search-wrap">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>
                <input id="blog-search" type="search" placeholder="search the journal&hellip;" autocomplete="off">
            </div>
            <div class="blog-chips">
{chips}
            </div>
        </div>
    </header>

    <main class="blog-timeline">
        <div class="section-inner">
            <div class="blog-timeline-rail">
{cards}
{empty}
                <p class="blog-empty" id="blog-no-results" style="display:none">nothing matches that search — try another word.</p>
            </div>
        </div>
    </main>

{footer}
{search_js}
</body>
</html>
""".format(head=head_html(cfg, cfg["blog_title"] + " — essays on journaling & mental health",
                          cfg["blog_description"], cfg["site_url"] + "/blog/"),
           nav=nav_html(cfg), footer=footer_html(cfg),
           blog_title=esc(cfg["blog_title"]), blog_desc=esc(cfg["blog_description"]),
           chips=chips, cards="\n".join(cards), empty=empty, search_js=search_js)


def render_posts_json(cfg, posts):
    return json.dumps([{
        "title": p["title"],
        "slug": p["slug"],
        "url": "{}/blog/{}/".format(cfg["site_url"], p["slug"]),
        "date": p["date"],
        "description": p["description"],
        "tags": p["tags"],
        "reading_minutes": p["reading_minutes"],
        "word_count": p["word_count"],
        "sources": p["sources"],
    } for p in posts], indent=2, ensure_ascii=False) + "\n"


def render_feed(cfg, posts):
    def rfc2822(iso):
        return format_datetime(
            datetime.strptime(iso, "%Y-%m-%d").replace(hour=12, tzinfo=timezone.utc))

    items = "\n".join("""    <item>
      <title>{title}</title>
      <link>{url}</link>
      <guid isPermaLink="true">{url}</guid>
      <pubDate>{date}</pubDate>
      <description>{desc}</description>
    </item>""".format(title=esc(p["title"]),
                      url="{}/blog/{}/".format(cfg["site_url"], p["slug"]),
                      date=rfc2822(p["date"]), desc=esc(p["description"]))
                      for p in posts)
    last_build = rfc2822(posts[0]["date"]) if posts else rfc2822("2026-01-01")
    return """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">
  <channel>
    <title>{title}</title>
    <link>{site}/blog/</link>
    <atom:link href="{site}/blog/feed.xml" rel="self" type="application/rss+xml"/>
    <description>{desc}</description>
    <language>en-us</language>
    <lastBuildDate>{last_build}</lastBuildDate>
{items}
  </channel>
</rss>
""".format(title=esc(cfg["blog_title"]), site=cfg["site_url"],
           desc=esc(cfg["blog_description"]), last_build=last_build, items=items)


def render_sitemap(cfg, posts):
    static_pages = ["", "privacy.html", "terms.html", "support.html", "blog/"]
    urls = ["""  <url>
    <loc>{}/{}</loc>
  </url>""".format(cfg["site_url"], page) for page in static_pages]
    urls += ["""  <url>
    <loc>{site}/blog/{slug}/</loc>
    <lastmod>{date}</lastmod>
  </url>""".format(site=cfg["site_url"], slug=p["slug"], date=p["date"])
             for p in posts]
    return ("""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
{}
</urlset>
""".format("\n".join(urls)))


def render_robots(cfg):
    return """User-agent: *
Allow: /

Sitemap: {}/sitemap.xml
""".format(cfg["site_url"])


def render_llms_txt(cfg, posts):
    post_lines = "\n".join(
        "- [{}]({}/blog/{}/): {}".format(p["title"], cfg["site_url"], p["slug"],
                                          p["description"])
        for p in posts)
    return """# Joice

> {blurb}

Joice is available for iOS. Download: {app_url}

## Blog — {blog_title}

{blog_desc}
Machine-readable index: {site}/blog/posts.json
RSS: {site}/blog/feed.xml

{posts}

## Pages

- [Home]({site}/): what Joice does and how it works
- [Support]({site}/support.html): help center and contact
- [Privacy]({site}/privacy.html): privacy policy
- [Terms]({site}/terms.html): terms of service
""".format(blurb=" ".join(cfg["brand_blurb"].split()), app_url=cfg["app_store_url"],
           blog_title=cfg["blog_title"], blog_desc=cfg["blog_description"],
           site=cfg["site_url"], posts=post_lines or "(first posts coming soon)")


def main():
    cfg = load_config()
    POSTS_DIR.mkdir(exist_ok=True)
    posts = sorted((parse_post(p) for p in POSTS_DIR.glob("*.md")),
                   key=lambda p: (p["date"], p["slug"]), reverse=True)

    slugs = [p["slug"] for p in posts]
    dupes = {s for s in slugs if slugs.count(s) > 1}
    if dupes:
        raise SystemExit("duplicate slugs: {}".format(", ".join(dupes)))

    BLOG_DIR.mkdir(exist_ok=True)
    for post in posts:
        out_dir = BLOG_DIR / post["slug"]
        out_dir.mkdir(exist_ok=True)
        (out_dir / "index.html").write_text(render_post_page(cfg, post))

    # Remove pages whose source markdown is gone
    for child in BLOG_DIR.iterdir():
        if child.is_dir() and child.name not in slugs:
            page = child / "index.html"
            if page.exists():
                page.unlink()
            try:
                child.rmdir()
            except OSError:
                pass

    (BLOG_DIR / "index.html").write_text(render_index_page(cfg, posts))
    (BLOG_DIR / "posts.json").write_text(render_posts_json(cfg, posts))
    (BLOG_DIR / "feed.xml").write_text(render_feed(cfg, posts))
    (ROOT / "sitemap.xml").write_text(render_sitemap(cfg, posts))
    (ROOT / "robots.txt").write_text(render_robots(cfg))
    (ROOT / "llms.txt").write_text(render_llms_txt(cfg, posts))

    print("built {} post(s) -> blog/, posts.json, feed.xml, sitemap.xml, "
          "robots.txt, llms.txt".format(len(posts)))


if __name__ == "__main__":
    main()
