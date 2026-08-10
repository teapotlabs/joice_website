#!/usr/bin/env python3
"""Process rewrite and revision requests from the review console + SEO plan.

Two queues, both fed by joiceapp.com/review/ and the Sunday SEO job:

1. Drafts flagged status='rewrite_requested' are rewritten using the
   reviewer's notes as mandatory direction, then returned to
   status='draft' for another look (unchanged legacy flow).

2. Published posts flagged revision_requested=true get a *targeted
   revision*: the post stays live and untouched; the revised version
   lands in pending_revision for apply/discard in the review console.
   The brief (revision_notes) is either reviewer notes or the Sunday
   job's SEO diagnosis.

Runs hourly via .github/workflows/blog-rewrite.yml. Exits immediately when
both queues are empty.

Environment:
  ANTHROPIC_API_KEY      Claude API key
  SUPABASE_SECRET_KEY    Supabase secret (service) key — write access
"""

import datetime as dt
import json
import os
import sys

import anthropic
import requests

from generate_post import (
    POST_SCHEMA, WRITER_MODEL, collect_text, format_by_key, format_limits,
    format_section, load_config, load_effective_style_guide, log_seo_event,
    stream_message, supabase_headers, validate,
)


def fetch_rewrite_queue(cfg):
    r = requests.get(
        cfg["supabase_url"] + "/rest/v1/posts",
        params={
            "select": "id,slug,title,description,body_md,tags,sources,"
                      "review_notes,format",
            "status": "eq.rewrite_requested",
        },
        headers=supabase_headers(cfg),
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def run_rewrite(client, cfg, style_guide, post, fmt, feedback=None):
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
        "piece below and requested a rewrite. Their notes are the brief — "
        "address every point in them.\n\n"
        "REVIEWER NOTES (mandatory direction):\n{notes}\n\n"
        "STYLE GUIDE (still applies in full):\n{style}\n"
        "{format_section}\n"
        "CURRENT PIECE:\n{current}\n\n"
        "{feedback}"
        "Rules for the rewrite:\n"
        "- Address the reviewer's notes above everything else.\n"
        "- Keep the piece's format unless the notes say otherwise.\n"
        "- Keep the slug exactly '{slug}'.\n"
        "- Cite only URLs already present in the piece's body or sources list — "
        "never invent a new source, statistic, or study finding.\n"
        "- Keep Joice mentioned once or twice, linking to {app_url}.\n"
        "- Keep the piece between {min_words} and {max_words} words.\n\n"
        "Return the full rewritten piece in the same JSON shape."
    ).format(site=cfg["site_name"], notes=post.get("review_notes") or "(none provided)",
             style=style_guide, format_section=format_section(fmt),
             current=json.dumps(current, indent=2),
             feedback=("PREVIOUS VALIDATION FAILURE — you must fix this: {}\n\n".format(feedback)
                       if feedback else ""),
             slug=post["slug"], app_url=cfg["app_store_url"],
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


def fetch_revision_queue(cfg):
    r = requests.get(
        cfg["supabase_url"] + "/rest/v1/posts",
        params={
            "select": "id,slug,title,description,body_md,tags,sources,"
                      "revision_notes,format",
            "status": "eq.published",
            "revision_requested": "is.true",
        },
        headers=supabase_headers(cfg),
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def run_revision(client, cfg, style_guide, post, fmt, feedback=None):
    """Targeted revision of a live post — surgical edits, not a rewrite."""
    current = {
        "title": post["title"],
        "slug": post["slug"],
        "description": post["description"],
        "tags": post["tags"],
        "body_markdown": post["body_md"],
        "sources": post["sources"],
    }
    prompt = (
        "You are the editor for the blog of {site}. The piece below is LIVE "
        "and mostly working — it needs targeted changes, not a rewrite. The "
        "brief may be reviewer notes or an SEO diagnosis with concrete "
        "actions; do exactly what it asks and leave everything else alone.\n\n"
        "REVISION BRIEF (mandatory direction):\n{notes}\n\n"
        "STYLE GUIDE (still applies in full):\n{style}\n"
        "{format_section}\n"
        "CURRENT PIECE:\n{current}\n\n"
        "{feedback}"
        "Rules:\n"
        "- Make only the changes the brief calls for, plus whatever tiny "
        "surrounding edits they force. Preserve the piece's voice and "
        "structure everywhere else.\n"
        "- Keep the slug exactly '{slug}'.\n"
        "- Cite only URLs already present in the piece's body or sources list — "
        "never invent a new source, statistic, or study finding.\n"
        "- Keep Joice mentioned once or twice, linking to {app_url}.\n"
        "- Keep the piece between {min_words} and {max_words} words.\n\n"
        "Return the full revised piece in the same JSON shape."
    ).format(site=cfg["site_name"], notes=post.get("revision_notes") or "(none provided)",
             style=style_guide, format_section=format_section(fmt),
             current=json.dumps(current, indent=2),
             feedback=("PREVIOUS VALIDATION FAILURE — you must fix this: {}\n\n".format(feedback)
                       if feedback else ""),
             slug=post["slug"], app_url=cfg["app_store_url"],
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


def save_revision(cfg, post, revised):
    """Store the revision alongside the live post; the console applies it."""
    r = requests.patch(
        cfg["supabase_url"] + "/rest/v1/posts",
        params={"id": "eq." + post["id"]},
        headers={**supabase_headers(cfg), "Prefer": "return=representation"},
        json={
            "pending_revision": {
                "title": revised["title"],
                "description": revised["description"],
                "body_markdown": revised["body_markdown"].strip(),
                "tags": revised["tags"],
                "sources": revised["sources"],
                "notes": post.get("revision_notes"),
                "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            },
            "revision_requested": False,
        },
        timeout=30,
    )
    r.raise_for_status()
    return r.json()[0]


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
    revisions = fetch_revision_queue(cfg)
    if not queue and not revisions:
        print("rewrite + revision queues are empty; nothing to do.")
        return

    style_guide = load_effective_style_guide(cfg)
    client = anthropic.Anthropic()
    failures = []

    for post in revisions:
        print("revising live post '{}' ({})...".format(
            post["title"], post["slug"]), flush=True)
        fmt = format_by_key(cfg, post.get("format"))
        eff = format_limits(cfg, fmt)
        revised = run_revision(client, eff, style_guide, post, fmt)
        revised["slug"] = post["slug"]  # never allow slug drift
        problems = validate(revised, eff)
        if problems:
            print("  validation failed, retrying: {}".format("; ".join(problems)), flush=True)
            revised = run_revision(client, eff, style_guide, post, fmt,
                                   feedback="; ".join(problems))
            revised["slug"] = post["slug"]
            problems = validate(revised, eff)
        if problems:
            failures.append((post["slug"], problems))
            print("  FAILED validation twice; leaving in queue.", file=sys.stderr)
            continue

        save_revision(cfg, post, revised)
        log_seo_event(cfg, post["id"], post["slug"], "revision_ready",
                      detail=(post.get("revision_notes") or "")[:200])
        print("  done -> pending revision awaiting apply/discard in the console")

        summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary_path:
            with open(summary_path, "a") as f:
                f.write("## Revision ready\n\n**{}** (`{}`) — post is still live; "
                        "apply or discard at https://joiceapp.com/review/\n\n".format(
                            post["title"], post["slug"]))

    for post in queue:
        print("rewriting '{}' ({})...".format(post["title"], post["slug"]), flush=True)
        fmt = format_by_key(cfg, post.get("format"))
        eff = format_limits(cfg, fmt)
        rewritten = run_rewrite(client, eff, style_guide, post, fmt)
        rewritten["slug"] = post["slug"]  # never allow slug drift
        problems = validate(rewritten, eff)
        if problems:
            print("  validation failed, retrying: {}".format("; ".join(problems)), flush=True)
            rewritten = run_rewrite(client, eff, style_guide, post, fmt,
                                    feedback="; ".join(problems))
            rewritten["slug"] = post["slug"]
            problems = validate(rewritten, eff)
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
