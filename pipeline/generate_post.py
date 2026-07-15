#!/usr/bin/env python3
"""Generate one blog post for joiceapp.com and upload it to Supabase.

Each run first picks an article format (essay, listicle, news commentary,
question, myth busting — weighted in config.yml, never repeating the previous
post's format; override with --format), then runs a three-phase pipeline
against the Claude API:
  1. research — pick a fresh topic fitting the format (or use --topic) and
     gather real sources via the server-side web search tool
  2. draft    — write the piece following the style guide + format brief,
     citing only researched sources
  3. polish   — an editor pass that hunts AI tells, verifies citation use,
     pressure-tests the headline, and enforces the 1-2 Joice plugs

Delivery: the finished post is INSERTed into the Supabase `posts` table as a
draft. A human reviews it in the Supabase Table Editor and flips `status` to
'published' — the website renders straight from the table, so publishing is
instant and needs no deploy.

Environment:
  ANTHROPIC_API_KEY      Claude API key
  SUPABASE_SECRET_KEY    Supabase secret (service) key — write access
"""

import argparse
import json
import os
import random
import re
import sys
import time
from pathlib import Path

import anthropic
import httpx
import requests
import yaml

PIPELINE_DIR = Path(__file__).resolve().parent

# Research (topic hunting + web search) and guidance distilling run on Opus;
# all reader-facing prose (draft, polish, rewrites) is written by Fable.
RESEARCH_MODEL = "claude-opus-4-8"
WRITER_MODEL = "claude-fable-5"

POST_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "Lowercase, curiosity-making headline, <=60 chars"},
        "slug": {"type": "string", "description": "kebab-case url slug"},
        "description": {"type": "string", "description": "~150 char meta description"},
        "tags": {"type": "array", "items": {"type": "string"}},
        "body_markdown": {"type": "string", "description": "Full essay body in markdown, no frontmatter, no H1 title"},
        "sources": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "url": {"type": "string"},
                    "publisher": {"type": "string"},
                },
                "required": ["title", "url", "publisher"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["title", "slug", "description", "tags", "body_markdown", "sources"],
    "additionalProperties": False,
}


def load_config():
    with open(PIPELINE_DIR / "config.yml") as f:
        return yaml.safe_load(f)


def load_style_guide():
    return (PIPELINE_DIR / "style_guide.md").read_text()


def load_effective_style_guide(cfg):
    """Style guide plus the active standing guidance distilled from reviewer
    notes (versioned in Supabase; managed from joiceapp.com/review/)."""
    guide = load_style_guide()
    if not os.environ.get("SUPABASE_SECRET_KEY"):
        return guide
    r = requests.get(
        cfg["supabase_url"] + "/rest/v1/prompt_guidance",
        params={"select": "version,content", "is_active": "eq.true", "limit": "1"},
        headers=supabase_headers(cfg),
        timeout=30,
    )
    r.raise_for_status()
    rows = r.json()
    if not rows:
        return guide
    return (guide + "\n\n## standing editorial guidance (v{}, distilled from "
            "the human editor's notes — part of the style guide)\n\n{}\n".format(
                rows[0]["version"], rows[0]["content"]))


# ------------------------------------------------------------- formats

def format_by_key(cfg, key):
    return next((f for f in cfg.get("formats") or [] if f["key"] == key), None)


def pick_format(cfg, override=None, last_format=None):
    """Choose this run's article format: weighted random over cfg['formats'],
    never repeating the most recent post's format back-to-back."""
    formats = cfg.get("formats") or []
    if not formats:
        return None
    if override:
        fmt = format_by_key(cfg, override)
        if not fmt:
            sys.exit("unknown format '{}'; known: {}".format(
                override, ", ".join(f["key"] for f in formats)))
        return fmt
    pool = [f for f in formats if f["key"] != last_format] or formats
    return random.choices(pool, weights=[f.get("weight", 1) for f in pool])[0]


def format_limits(cfg, fmt):
    """Config copy with the format's word/source overrides applied."""
    eff = dict(cfg)
    for key in ("min_words", "max_words", "min_sources"):
        if fmt and key in fmt:
            eff[key] = fmt[key]
    return eff


def format_section(fmt):
    """Prompt block describing this piece's format, for all three passes."""
    if not fmt:
        return ""
    return ("\nFORMAT for this piece — {key}. Where this brief conflicts with "
            "the style guide's structure rules, the brief wins:\n{brief}\n"
            .format(key=fmt["key"], brief=fmt["brief"].strip()))


# ------------------------------------------------------------- supabase i/o

def supabase_headers(cfg):
    key = os.environ.get("SUPABASE_SECRET_KEY")
    if not key:
        sys.exit("SUPABASE_SECRET_KEY is not set")
    return {
        "apikey": key,
        "Authorization": "Bearer " + key,
        "Content-Type": "application/json",
    }


def existing_posts(cfg):
    """All posts (drafts included), newest first, for topic dedupe, slug
    uniqueness, and last-format avoidance."""
    if not os.environ.get("SUPABASE_SECRET_KEY"):
        print("warning: SUPABASE_SECRET_KEY unset; skipping topic dedupe",
              file=sys.stderr)
        return []
    r = requests.get(
        cfg["supabase_url"] + "/rest/v1/posts",
        params={"select": "slug,title,tags,format", "order": "created_at.desc"},
        headers=supabase_headers(cfg),
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def reviewer_feedback(cfg):
    """Recent notes from the human reviewer (joiceapp.com/review/), injected
    into the writing prompts as standing editorial guidance."""
    if not os.environ.get("SUPABASE_SECRET_KEY"):
        return ""
    r = requests.get(
        cfg["supabase_url"] + "/rest/v1/posts",
        params={"select": "title,review_notes", "review_notes": "not.is.null",
                "order": "updated_at.desc", "limit": "10"},
        headers=supabase_headers(cfg),
        timeout=30,
    )
    r.raise_for_status()
    notes = [(p["title"], p["review_notes"].strip())
             for p in r.json() if (p.get("review_notes") or "").strip()]
    if not notes:
        return ""
    lines = "\n".join('- on "{}": {}'.format(t, n) for t, n in notes)
    return ("\nREVIEWER FEEDBACK — notes the human editor left on previous "
            "essays. Treat them as standing guidance and apply them to this "
            "essay too:\n{}\n".format(lines))


def unique_slug(slug, taken):
    candidate, n = slug, 2
    while candidate in taken:
        candidate = "{}-{}".format(slug, n)
        n += 1
    return candidate


def insert_draft(cfg, post, taken_slugs, fmt=None):
    row = {
        "slug": unique_slug(post["slug"], taken_slugs),
        "title": post["title"],
        "description": post["description"],
        "body_md": post["body_markdown"].strip(),
        "tags": post["tags"],
        "sources": post["sources"],
        "status": "draft",
        "format": fmt["key"] if fmt else None,
    }
    r = requests.post(
        cfg["supabase_url"] + "/rest/v1/posts",
        headers={**supabase_headers(cfg), "Prefer": "return=representation"},
        json=row,
        timeout=30,
    )
    r.raise_for_status()
    return r.json()[0]


def save_research(cfg, post_id, research_brief):
    """Archive the research pass output alongside the draft (post_research
    table; service-role only, not exposed to the public site)."""
    r = requests.post(
        cfg["supabase_url"] + "/rest/v1/post_research",
        headers=supabase_headers(cfg),
        json={"post_id": post_id, "research_md": research_brief,
              "model": RESEARCH_MODEL},
        timeout=30,
    )
    r.raise_for_status()


# ------------------------------------------------------------- generation

def collect_text(message):
    return "\n".join(b.text for b in message.content if b.type == "text")


# Waits between attempts: long enough to ride out an API brownout (both
# scheduled runs lost on 2026-07-13/14 died on transient mid-stream errors
# at peak-load hours). Total worst case ~3h10m, within the 6h job limit.
RETRY_DELAYS = (600, 3600, 7200)


def _is_retryable(exc):
    # Mid-stream error events (e.g. overloaded_error) surface as
    # APIStatusError carrying the original 200 response, so only true
    # client errors are treated as permanent.
    status = getattr(exc, "status_code", None)
    if status is not None and 400 <= status < 500:
        return status in (408, 429)
    return True


def stream_message(client, **kwargs):
    for attempt, delay in enumerate(RETRY_DELAYS + (None,), start=1):
        try:
            with client.messages.stream(**kwargs) as stream:
                return stream.get_final_message()
        except (anthropic.AnthropicError, httpx.HTTPError) as e:
            if delay is None or not _is_retryable(e):
                raise
            print("API call failed ({}: {}); attempt {}/{}, retrying in {} min"
                  .format(type(e).__name__, e, attempt, len(RETRY_DELAYS) + 1,
                          delay // 60), flush=True)
            time.sleep(delay)


def run_research(client, cfg, topic_override, fmt, existing):
    """Web-search research pass. Returns a research brief (markdown/text)."""
    covered = "\n".join(
        "- {}{} (tags: {})".format(
            p.get("title", "?"),
            " [{}]".format(p["format"]) if p.get("format") else "",
            ", ".join(p.get("tags") or []))
        for p in existing
    ) or "- (none yet — this is the first post)"

    if topic_override:
        topic_instruction = "Research this specific topic: {}".format(topic_override)
    else:
        pillars = "\n".join("- " + p for p in cfg["pillars"])
        topic_instruction = (
            "First, pick ONE specific, searchable topic that fits this piece's "
            "format (described below). It must sit inside these content "
            "pillars:\n{}\n\n"
            "It must NOT overlap with anything already covered:\n{}\n\n"
            "Prefer a specific angle over a broad survey (e.g. 'why you rehearse "
            "conversations in the shower' beats 'the benefits of self-reflection'). "
            "Favor topics people actually search for."
        ).format(pillars, covered)

    prompt = (
        "You are the researcher for the blog of {site} ({blurb}).\n\n"
        "{topic_instruction}\n"
        "{format_section}\n"
        "Then research it with web search. Find 4-8 high-quality sources: "
        "peer-reviewed studies, university write-ups, or reputable publications. "
        "For every source record the exact title, publisher, working URL, and the "
        "specific claim it supports. Prefer primary sources and named researchers. "
        "Gather whatever the format needs — e.g. enough real, distinct items to "
        "fill a list piece, or the primary document behind a news piece.\n\n"
        "Return a research brief containing:\n"
        "1. TOPIC: the chosen topic and angle in one sentence\n"
        "2. WHY NOW: why readers search for / care about this\n"
        "3. KEY CLAIMS: each factual claim paired with its supporting source URL\n"
        "4. SOURCES: the full list (title | publisher | url)\n"
        "5. NARRATIVE IDEAS: 2-3 concrete scenes or hooks a writer could open with\n\n"
        "Only include URLs that appeared in your actual search results. Never invent "
        "a URL, a statistic, or a study finding."
    ).format(site=cfg["site_name"], blurb=cfg["brand_blurb"].strip(),
             topic_instruction=topic_instruction, format_section=format_section(fmt))

    messages = [{"role": "user", "content": prompt}]
    for _ in range(6):
        response = stream_message(
            client,
            model=RESEARCH_MODEL,
            max_tokens=20000,
            thinking={"type": "adaptive"},
            tools=[{"type": "web_search_20260209", "name": "web_search", "max_uses": 10}],
            messages=messages,
        )
        if response.stop_reason != "pause_turn":
            break
        # Server-side tool loop paused; resume where it left off.
        messages = messages[:1] + [{"role": "assistant", "content": response.content}]
    return collect_text(response)


def run_draft(client, cfg, style_guide, research_brief, fmt, standing_feedback=""):
    prompt = (
        "You write for the blog of {site}. Brand context: {blurb}\n\n"
        "STYLE GUIDE (follow it exactly):\n{style}\n"
        "{format_section}"
        "{standing_feedback}\n"
        "RESEARCH BRIEF (cite only these sources; never invent facts or URLs):\n"
        "{research}\n\n"
        "Write the full piece now. Requirements:\n"
        "- {min_words}-{max_words} words\n"
        "- every factual claim carries an inline markdown link to its source from "
        "the brief\n"
        "- mention Joice exactly once or twice, each time linking to "
        "{app_url} — natural asides, not ads\n"
        "- title: draft five candidates per the style guide's headlines section, "
        "output only the one a stranger would click; lowercase, <=60 chars\n"
        "- slug kebab-case; description ~150 chars\n"
        "- body_markdown must not repeat the title as a heading"
    ).format(site=cfg["site_name"], blurb=cfg["brand_blurb"].strip(),
             style=style_guide, research=research_brief,
             format_section=format_section(fmt),
             standing_feedback=standing_feedback,
             min_words=cfg["min_words"], max_words=cfg["max_words"],
             app_url=cfg["app_store_url"])

    response = stream_message(
        client,
        model=WRITER_MODEL,
        max_tokens=32000,
        thinking={"type": "adaptive"},
        output_config={"format": {"type": "json_schema", "schema": POST_SCHEMA}},
        messages=[{"role": "user", "content": prompt}],
    )
    return json.loads(collect_text(response))


def run_polish(client, cfg, style_guide, research_brief, draft, fmt, feedback=None,
               standing_feedback=""):
    prompt = (
        "You are the editor for the blog of {site}. Below is a draft piece as JSON, "
        "the research brief it must stay grounded in, and the style guide.\n\n"
        "STYLE GUIDE:\n{style}\n"
        "{format_section}"
        "{standing_feedback}\n"
        "RESEARCH BRIEF:\n{research}\n\n"
        "DRAFT:\n{draft}\n\n"
        "{feedback}"
        "Edit ruthlessly:\n"
        "1. Kill every banned tell from the style guide; rewrite any sentence that "
        "sounds machine-written; vary paragraph and sentence rhythm.\n"
        "2. Check every inline link exists in the research brief's source list. "
        "Remove or rewrite any claim whose source isn't there.\n"
        "3. Ensure Joice is mentioned exactly once or twice with the link {app_url}, "
        "reading as a natural aside.\n"
        "4. Confirm the piece actually delivers its format brief — don't flatten "
        "a list into prose or strip the opinion out of a commentary.\n"
        "5. Pressure-test the title against the style guide's headlines section: "
        "would a stranger click it? If it's merely accurate, rewrite it until "
        "it's curious — without over-promising.\n"
        "6. Tighten the opening; cut throat-clearing; make the ending land.\n"
        "7. Keep the piece between {min_words} and {max_words} words.\n\n"
        "Return the final piece in the same JSON shape, with the sources array "
        "listing only sources actually linked in the body."
    ).format(site=cfg["site_name"], style=style_guide, research=research_brief,
             format_section=format_section(fmt),
             draft=json.dumps(draft, indent=2),
             feedback=("PREVIOUS VALIDATION FAILURE — you must fix this: {}\n\n".format(feedback)
                       if feedback else ""),
             standing_feedback=standing_feedback,
             app_url=cfg["app_store_url"],
             min_words=cfg["min_words"], max_words=cfg["max_words"])

    response = stream_message(
        client,
        model=WRITER_MODEL,
        max_tokens=32000,
        thinking={"type": "adaptive"},
        output_config={"format": {"type": "json_schema", "schema": POST_SCHEMA}},
        messages=[{"role": "user", "content": prompt}],
    )
    return json.loads(collect_text(response))


def validate(post, cfg):
    """Return a list of human-readable problems (empty = valid)."""
    problems = []
    body = post["body_markdown"]

    plug_count = body.count(cfg["app_store_url"])
    if not 1 <= plug_count <= 2:
        problems.append(
            "Joice app link ({}) appears {} times in the body; must be 1 or 2.".format(
                cfg["app_store_url"], plug_count))

    real_sources = [s for s in post["sources"] if s["url"].startswith("http")]
    if len(real_sources) < cfg["min_sources"]:
        problems.append("Only {} sources with http URLs; need at least {}.".format(
            len(real_sources), cfg["min_sources"]))

    inline_links = set(re.findall(r"\]\((https?://[^)\s]+)\)", body))
    cited = [u for u in inline_links if u != cfg["app_store_url"]]
    if len(cited) < cfg["min_sources"]:
        problems.append("Only {} inline source links in the body; need at least {}.".format(
            len(cited), cfg["min_sources"]))

    words = len(re.findall(r"\w+", body))
    if words < cfg["min_words"] * 0.75:
        problems.append("Essay is {} words; too short (minimum ~{}).".format(
            words, cfg["min_words"]))

    if not re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", post["slug"]):
        problems.append("Slug '{}' is not kebab-case.".format(post["slug"]))

    banned = ["delve", "tapestry", "in today's fast-paced world",
              "navigate the complexities", "game-changer", "embark on a journey"]
    lower = body.lower()
    hits = [w for w in banned if w in lower]
    if hits:
        problems.append("Banned AI-tell phrases present: {}".format(", ".join(hits)))

    return problems


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--topic", help="Override automatic topic selection")
    parser.add_argument("--format", help="Override automatic format selection "
                                         "(a key from config.yml formats)")
    parser.add_argument("--dry-run", metavar="OUT.json",
                        help="Write the finished post to a JSON file instead "
                             "of uploading it to Supabase")
    args = parser.parse_args()

    cfg = load_config()
    style_guide = load_effective_style_guide(cfg)
    client = anthropic.Anthropic()

    existing = existing_posts(cfg)
    last_format = existing[0].get("format") if existing else None
    fmt = pick_format(cfg, args.format, last_format)
    if fmt:
        print("format: {} (previous post: {})".format(
            fmt["key"], last_format or "unknown"), flush=True)
        cfg = format_limits(cfg, fmt)

    print("[1/3] researching...", flush=True)
    research_brief = run_research(client, cfg, args.topic, fmt, existing)
    print(research_brief[:600], "...\n", flush=True)

    standing = reviewer_feedback(cfg)
    if standing:
        print("(applying reviewer feedback from {} previous note(s))".format(
            standing.count("\n- on ")), flush=True)

    print("[2/3] drafting...", flush=True)
    draft = run_draft(client, cfg, style_guide, research_brief, fmt, standing)
    print("draft: {!r} ({} words)".format(
        draft["title"], len(draft["body_markdown"].split())), flush=True)

    print("[3/3] polishing...", flush=True)
    post = run_polish(client, cfg, style_guide, research_brief, draft, fmt,
                      standing_feedback=standing)

    problems = validate(post, cfg)
    if problems:
        print("validation failed, retrying polish with feedback:\n  " +
              "\n  ".join(problems), flush=True)
        post = run_polish(client, cfg, style_guide, research_brief, post, fmt,
                          feedback="; ".join(problems), standing_feedback=standing)
        problems = validate(post, cfg)

    if problems:
        print("FATAL: post failed validation twice:", file=sys.stderr)
        for p in problems:
            print("  - " + p, file=sys.stderr)
        sys.exit(1)

    if args.dry_run:
        with open(args.dry_run, "w") as f:
            json.dump({**post, "research_markdown": research_brief},
                      f, indent=2, ensure_ascii=False)
        print("dry run: wrote {} (not uploaded)".format(args.dry_run))
        print("title: {} [{}]".format(post["title"], fmt["key"] if fmt else "-"))
        return

    taken = {p["slug"] for p in existing_posts(cfg)}
    row = insert_draft(cfg, post, taken, fmt)
    save_research(cfg, row["id"], research_brief)
    print("uploaded draft '{}' (slug: {})".format(row["title"], row["slug"]))
    print("review + publish: flip status to 'published' in the Supabase Table "
          "Editor and it is live immediately.")

    # surface details in the GitHub Actions job summary, if running in CI
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a") as f:
            f.write(
                "## Draft ready for review\n\n"
                "**{title}**\n\n{desc}\n\n"
                "- format: {fmt}\n"
                "- slug: `{slug}`\n"
                "- tags: {tags}\n"
                "- sources: {nsources}\n\n"
                "Review it in the [Supabase Table Editor]"
                "(https://supabase.com/dashboard/project/{ref}/editor) and set "
                "`status` to `published` to make it live at "
                "{site}/blog/{slug}/\n".format(
                    title=row["title"], desc=row["description"], slug=row["slug"],
                    fmt=fmt["key"] if fmt else "-",
                    tags=", ".join(post["tags"]), nsources=len(post["sources"]),
                    ref=cfg["supabase_url"].split("//")[1].split(".")[0],
                    site=cfg["site_url"]))


if __name__ == "__main__":
    main()
