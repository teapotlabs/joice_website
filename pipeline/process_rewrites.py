#!/usr/bin/env python3
"""Process rewrite requests from the review console.

Posts flagged status='rewrite_requested' (via joiceapp.com/review/) are
rewritten by Claude using the reviewer's notes as mandatory editorial
direction, then returned to status='draft' for another look. Slug and
sources are preserved; title/description/body/tags may change.

Runs hourly via .github/workflows/blog-rewrite.yml. Exits immediately when
the queue is empty.

Environment:
  ANTHROPIC_API_KEY      Claude API key
  SUPABASE_SECRET_KEY    Supabase secret (service) key — write access
"""

import json
import os
import sys

import anthropic
import requests

from generate_post import (
    MODEL, POST_SCHEMA, collect_text, load_config, load_style_guide,
    stream_message, supabase_headers, validate,
)


def fetch_rewrite_queue(cfg):
    r = requests.get(
        cfg["supabase_url"] + "/rest/v1/posts",
        params={
            "select": "id,slug,title,description,body_md,tags,sources,review_notes",
            "status": "eq.rewrite_requested",
        },
        headers=supabase_headers(cfg),
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def run_rewrite(client, cfg, style_guide, post, feedback=None):
    current = {
        "title": post["title"],
        "slug": post["slug"],
        "description": post["description"],
        "tags": post["tags"],
        "body_markdown": post["body_md"],
        "sources": post["sources"],
    }
    prompt = (
        "You are the editor for the blog of {site}. The human reviewer read the "
        "essay below and requested a rewrite. Their notes are the brief — "
        "address every point in them.\n\n"
        "REVIEWER NOTES (mandatory direction):\n{notes}\n\n"
        "STYLE GUIDE (still applies in full):\n{style}\n\n"
        "CURRENT ESSAY:\n{current}\n\n"
        "{feedback}"
        "Rules for the rewrite:\n"
        "- Address the reviewer's notes above everything else.\n"
        "- Keep the slug exactly '{slug}'.\n"
        "- Cite only URLs already present in the essay's body or sources list — "
        "never invent a new source, statistic, or study finding.\n"
        "- Keep Joice mentioned once or twice, linking to {app_url}.\n"
        "- Keep the essay between {min_words} and {max_words} words.\n\n"
        "Return the full rewritten essay in the same JSON shape."
    ).format(site=cfg["site_name"], notes=post.get("review_notes") or "(none provided)",
             style=style_guide, current=json.dumps(current, indent=2),
             feedback=("PREVIOUS VALIDATION FAILURE — you must fix this: {}\n\n".format(feedback)
                       if feedback else ""),
             slug=post["slug"], app_url=cfg["app_store_url"],
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


def save_rewrite(cfg, post_id, rewritten):
    r = requests.patch(
        cfg["supabase_url"] + "/rest/v1/posts",
        params={"id": "eq." + post_id},
        headers={**supabase_headers(cfg), "Prefer": "return=representation"},
        json={
            "title": rewritten["title"],
            "description": rewritten["description"],
            "body_md": rewritten["body_markdown"].strip(),
            "tags": rewritten["tags"],
            "sources": rewritten["sources"],
            "status": "draft",
        },
        timeout=30,
    )
    r.raise_for_status()
    return r.json()[0]


def main():
    cfg = load_config()
    queue = fetch_rewrite_queue(cfg)
    if not queue:
        print("rewrite queue is empty; nothing to do.")
        return

    style_guide = load_style_guide()
    client = anthropic.Anthropic()
    failures = []

    for post in queue:
        print("rewriting '{}' ({})...".format(post["title"], post["slug"]), flush=True)
        rewritten = run_rewrite(client, cfg, style_guide, post)
        rewritten["slug"] = post["slug"]  # never allow slug drift
        problems = validate(rewritten, cfg)
        if problems:
            print("  validation failed, retrying: {}".format("; ".join(problems)), flush=True)
            rewritten = run_rewrite(client, cfg, style_guide, post,
                                    feedback="; ".join(problems))
            rewritten["slug"] = post["slug"]
            problems = validate(rewritten, cfg)
        if problems:
            failures.append((post["slug"], problems))
            print("  FAILED validation twice; leaving in queue.", file=sys.stderr)
            continue

        row = save_rewrite(cfg, post["id"], rewritten)
        print("  done -> draft '{}' awaiting review".format(row["title"]))

        summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary_path:
            with open(summary_path, "a") as f:
                f.write("## Rewrite ready for review\n\n**{}** (`{}`)\n\n"
                        "Back in the queue at https://joiceapp.com/review/\n\n".format(
                            row["title"], row["slug"]))

    if failures:
        for slug, problems in failures:
            print("failed: {} — {}".format(slug, "; ".join(problems)), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
