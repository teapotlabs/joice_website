#!/usr/bin/env python3
"""Generate one blog post for joiceapp.com and upload it to Supabase.

Three-phase pipeline against the Claude API:
  1. research — pick a fresh topic (or use --topic) and gather real sources
     via the server-side web search tool
  2. draft    — write the essay following the style guide, citing only
     researched sources
  3. polish   — an editor pass that hunts AI tells, verifies citation use,
     and enforces the 1-2 Joice plugs

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
import re
import sys
from pathlib import Path

import anthropic
import requests
import yaml

PIPELINE_DIR = Path(__file__).resolve().parent

MODEL = "claude-opus-4-8"

POST_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "Lowercase, plain-language, <=60 chars"},
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
    """All posts (drafts included) for topic dedupe + slug uniqueness."""
    if not os.environ.get("SUPABASE_SECRET_KEY"):
        print("warning: SUPABASE_SECRET_KEY unset; skipping topic dedupe",
              file=sys.stderr)
        return []
    r = requests.get(
        cfg["supabase_url"] + "/rest/v1/posts",
        params={"select": "slug,title,tags"},
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


def insert_draft(cfg, post, taken_slugs):
    row = {
        "slug": unique_slug(post["slug"], taken_slugs),
        "title": post["title"],
        "description": post["description"],
        "body_md": post["body_markdown"].strip(),
        "tags": post["tags"],
        "sources": post["sources"],
        "status": "draft",
    }
    r = requests.post(
        cfg["supabase_url"] + "/rest/v1/posts",
        headers={**supabase_headers(cfg), "Prefer": "return=representation"},
        json=row,
        timeout=30,
    )
    r.raise_for_status()
    return r.json()[0]


# ------------------------------------------------------------- generation

def collect_text(message):
    return "\n".join(b.text for b in message.content if b.type == "text")


def stream_message(client, **kwargs):
    with client.messages.stream(**kwargs) as stream:
        return stream.get_final_message()


def run_research(client, cfg, topic_override):
    """Web-search research pass. Returns a research brief (markdown/text)."""
    existing = existing_posts(cfg)
    covered = "\n".join(
        "- {} (tags: {})".format(p.get("title", "?"), ", ".join(p.get("tags") or []))
        for p in existing
    ) or "- (none yet — this is the first post)"

    if topic_override:
        topic_instruction = "Research this specific topic: {}".format(topic_override)
    else:
        pillars = "\n".join("- " + p for p in cfg["pillars"])
        topic_instruction = (
            "First, pick ONE specific, searchable topic for a new essay. It must sit "
            "inside these content pillars:\n{}\n\n"
            "It must NOT overlap with anything already covered:\n{}\n\n"
            "Prefer a specific angle over a broad survey (e.g. 'why you rehearse "
            "conversations in the shower' beats 'the benefits of self-reflection'). "
            "Favor topics people actually search for."
        ).format(pillars, covered)

    prompt = (
        "You are the researcher for the blog of {site} ({blurb}).\n\n"
        "{topic_instruction}\n\n"
        "Then research it with web search. Find 4-8 high-quality sources: "
        "peer-reviewed studies, university write-ups, or reputable publications. "
        "For every source record the exact title, publisher, working URL, and the "
        "specific claim it supports. Prefer primary sources and named researchers.\n\n"
        "Return a research brief containing:\n"
        "1. TOPIC: the chosen topic and angle in one sentence\n"
        "2. WHY NOW: why readers search for / care about this\n"
        "3. KEY CLAIMS: each factual claim paired with its supporting source URL\n"
        "4. SOURCES: the full list (title | publisher | url)\n"
        "5. NARRATIVE IDEAS: 2-3 concrete scenes or hooks a writer could open with\n\n"
        "Only include URLs that appeared in your actual search results. Never invent "
        "a URL, a statistic, or a study finding."
    ).format(site=cfg["site_name"], blurb=cfg["brand_blurb"].strip(),
             topic_instruction=topic_instruction)

    messages = [{"role": "user", "content": prompt}]
    for _ in range(6):
        response = stream_message(
            client,
            model=MODEL,
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


def run_draft(client, cfg, style_guide, research_brief, standing_feedback=""):
    prompt = (
        "You write essays for the blog of {site}. Brand context: {blurb}\n\n"
        "STYLE GUIDE (follow it exactly):\n{style}\n"
        "{standing_feedback}\n"
        "RESEARCH BRIEF (cite only these sources; never invent facts or URLs):\n"
        "{research}\n\n"
        "Write the full essay now. Requirements:\n"
        "- {min_words}-{max_words} words\n"
        "- every factual claim carries an inline markdown link to its source from "
        "the brief\n"
        "- mention Joice exactly once or twice, each time linking to "
        "{app_url} — natural asides, not ads\n"
        "- title lowercase, <=60 chars; slug kebab-case; description ~150 chars\n"
        "- body_markdown must not repeat the title as a heading"
    ).format(site=cfg["site_name"], blurb=cfg["brand_blurb"].strip(),
             style=style_guide, research=research_brief,
             standing_feedback=standing_feedback,
             min_words=cfg["min_words"], max_words=cfg["max_words"],
             app_url=cfg["app_store_url"])

    response = stream_message(
        client,
        model=MODEL,
        max_tokens=32000,
        thinking={"type": "adaptive"},
        output_config={"format": {"type": "json_schema", "schema": POST_SCHEMA}},
        messages=[{"role": "user", "content": prompt}],
    )
    return json.loads(collect_text(response))


def run_polish(client, cfg, style_guide, research_brief, draft, feedback=None,
               standing_feedback=""):
    prompt = (
        "You are the editor for the blog of {site}. Below is a draft essay as JSON, "
        "the research brief it must stay grounded in, and the style guide.\n\n"
        "STYLE GUIDE:\n{style}\n"
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
        "4. Tighten the opening; cut throat-clearing; make the ending land.\n"
        "5. Keep the essay between {min_words} and {max_words} words.\n\n"
        "Return the final essay in the same JSON shape, with the sources array "
        "listing only sources actually linked in the body."
    ).format(site=cfg["site_name"], style=style_guide, research=research_brief,
             draft=json.dumps(draft, indent=2),
             feedback=("PREVIOUS VALIDATION FAILURE — you must fix this: {}\n\n".format(feedback)
                       if feedback else ""),
             standing_feedback=standing_feedback,
             app_url=cfg["app_store_url"],
             min_words=cfg["min_words"], max_words=cfg["max_words"])

    response = stream_message(
        client,
        model=MODEL,
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
    parser.add_argument("--dry-run", metavar="OUT.json",
                        help="Write the finished post to a JSON file instead "
                             "of uploading it to Supabase")
    args = parser.parse_args()

    cfg = load_config()
    style_guide = load_style_guide()
    client = anthropic.Anthropic()

    print("[1/3] researching...", flush=True)
    research_brief = run_research(client, cfg, args.topic)
    print(research_brief[:600], "...\n", flush=True)

    standing = reviewer_feedback(cfg)
    if standing:
        print("(applying reviewer feedback from {} previous note(s))".format(
            standing.count("\n- on ")), flush=True)

    print("[2/3] drafting...", flush=True)
    draft = run_draft(client, cfg, style_guide, research_brief, standing)
    print("draft: {!r} ({} words)".format(
        draft["title"], len(draft["body_markdown"].split())), flush=True)

    print("[3/3] polishing...", flush=True)
    post = run_polish(client, cfg, style_guide, research_brief, draft,
                      standing_feedback=standing)

    problems = validate(post, cfg)
    if problems:
        print("validation failed, retrying polish with feedback:\n  " +
              "\n  ".join(problems), flush=True)
        post = run_polish(client, cfg, style_guide, research_brief, post,
                          feedback="; ".join(problems), standing_feedback=standing)
        problems = validate(post, cfg)

    if problems:
        print("FATAL: post failed validation twice:", file=sys.stderr)
        for p in problems:
            print("  - " + p, file=sys.stderr)
        sys.exit(1)

    if args.dry_run:
        with open(args.dry_run, "w") as f:
            json.dump(post, f, indent=2, ensure_ascii=False)
        print("dry run: wrote {} (not uploaded)".format(args.dry_run))
        print("title: {}".format(post["title"]))
        return

    taken = {p["slug"] for p in existing_posts(cfg)}
    row = insert_draft(cfg, post, taken)
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
                "- slug: `{slug}`\n"
                "- tags: {tags}\n"
                "- sources: {nsources}\n\n"
                "Review it in the [Supabase Table Editor]"
                "(https://supabase.com/dashboard/project/{ref}/editor) and set "
                "`status` to `published` to make it live at "
                "{site}/blog/{slug}/\n".format(
                    title=row["title"], desc=row["description"], slug=row["slug"],
                    tags=", ".join(post["tags"]), nsources=len(post["sources"]),
                    ref=cfg["supabase_url"].split("//")[1].split(".")[0],
                    site=cfg["site_url"]))


if __name__ == "__main__":
    main()
