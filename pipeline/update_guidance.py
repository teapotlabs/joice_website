#!/usr/bin/env python3
"""Distill reviewer notes into versioned standing writing guidance.

Reads every reviewer note saved in the review console, and — when the notes
have changed since the last distillation — asks Claude to fold them into the
"standing editorial guidance" document. The result is inserted into
`prompt_guidance` as a new active version; older versions stay in the table
so any of them can be re-activated (reverted) from the review console.

The active version is appended to the style guide in every generation and
rewrite prompt (see load_effective_style_guide in generate_post.py).

Runs at the start of each blog-generate workflow. No-ops when there are no
notes or when nothing changed.

Environment:
  ANTHROPIC_API_KEY      Claude API key
  SUPABASE_SECRET_KEY    Supabase secret (service) key — write access
"""

import hashlib
import json
import os

import anthropic
import requests

from generate_post import (
    MODEL, collect_text, load_config, load_style_guide, stream_message,
    supabase_headers,
)

GUIDANCE_SCHEMA = {
    "type": "object",
    "properties": {
        "content": {"type": "string",
                    "description": "The full updated guidance document, plain text"},
        "changelog": {"type": "string",
                      "description": "One or two sentences on what changed and why"},
    },
    "required": ["content", "changelog"],
    "additionalProperties": False,
}


def fetch_notes(cfg):
    r = requests.get(
        cfg["supabase_url"] + "/rest/v1/posts",
        params={"select": "slug,title,review_notes", "review_notes": "not.is.null",
                "order": "created_at.asc"},
        headers=supabase_headers(cfg),
        timeout=30,
    )
    r.raise_for_status()
    return [(p["title"], p["review_notes"].strip())
            for p in r.json() if (p.get("review_notes") or "").strip()]


def fetch_active(cfg):
    r = requests.get(
        cfg["supabase_url"] + "/rest/v1/prompt_guidance",
        params={"select": "*", "is_active": "eq.true", "limit": "1"},
        headers=supabase_headers(cfg),
        timeout=30,
    )
    r.raise_for_status()
    rows = r.json()
    return rows[0] if rows else None


def next_version(cfg):
    r = requests.get(
        cfg["supabase_url"] + "/rest/v1/prompt_guidance",
        params={"select": "version", "order": "version.desc", "limit": "1"},
        headers=supabase_headers(cfg),
        timeout=30,
    )
    r.raise_for_status()
    rows = r.json()
    return (rows[0]["version"] if rows else 0) + 1


def insert_version(cfg, content, rationale, source_hash):
    headers = supabase_headers(cfg)
    # deactivate current, then insert the new active version
    requests.patch(
        cfg["supabase_url"] + "/rest/v1/prompt_guidance",
        params={"is_active": "eq.true"},
        headers=headers, json={"is_active": False}, timeout=30,
    ).raise_for_status()
    r = requests.post(
        cfg["supabase_url"] + "/rest/v1/prompt_guidance",
        headers={**headers, "Prefer": "return=representation"},
        json={"version": next_version(cfg), "content": content,
              "rationale": rationale, "source_hash": source_hash,
              "is_active": True, "created_by": "distiller"},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()[0]


def distill(client, cfg, current_content, notes):
    notes_block = "\n".join('- on "{}": {}'.format(t, n) for t, n in notes)
    prompt = (
        "You maintain the STANDING EDITORIAL GUIDANCE for the blog of {site} — "
        "a short, durable document appended to the writer's style guide in "
        "every prompt. The human editor leaves notes on individual posts; your "
        "job is to fold the lessons in those notes into the guidance so future "
        "posts get them right the first time.\n\n"
        "BASE STYLE GUIDE (already covers voice, structure, banned tells, "
        "citations — do NOT repeat anything it already says):\n{style}\n\n"
        "CURRENT STANDING GUIDANCE:\n{current}\n\n"
        "ALL REVIEWER NOTES TO DATE:\n{notes}\n\n"
        "Produce the updated guidance document:\n"
        "- Keep only durable, generalizable rules — turn one-off fixes into "
        "the general lesson behind them.\n"
        "- Preserve existing guidance rules unless a newer note contradicts "
        "them (newer notes win).\n"
        "- Plain text, one rule per line starting with '- ', max ~25 lines. "
        "Terse and directive, written to a writer.\n"
        "- If the notes add nothing new, return the current guidance unchanged "
        "and say so in the changelog."
    ).format(site=cfg["site_name"], style=load_style_guide(),
             current=current_content or "(none yet)", notes=notes_block)

    response = stream_message(
        client,
        model=MODEL,
        max_tokens=8000,
        thinking={"type": "adaptive"},
        output_config={"format": {"type": "json_schema", "schema": GUIDANCE_SCHEMA}},
        messages=[{"role": "user", "content": prompt}],
    )
    return json.loads(collect_text(response))


def main():
    cfg = load_config()
    notes = fetch_notes(cfg)
    if not notes:
        print("no reviewer notes yet; guidance unchanged.")
        return

    source_hash = hashlib.sha256(
        json.dumps(sorted(notes), ensure_ascii=False).encode()).hexdigest()
    active = fetch_active(cfg)
    if active and active.get("source_hash") == source_hash:
        print("notes unchanged since guidance v{}; nothing to distill.".format(
            active["version"]))
        return

    print("distilling {} note(s) into guidance...".format(len(notes)), flush=True)
    client = anthropic.Anthropic()
    result = distill(client, cfg, active["content"] if active else "", notes)

    if active and result["content"].strip() == active["content"].strip():
        # content identical — just record that these notes are accounted for
        requests.patch(
            cfg["supabase_url"] + "/rest/v1/prompt_guidance",
            params={"id": "eq." + active["id"]},
            headers=supabase_headers(cfg),
            json={"source_hash": source_hash},
            timeout=30,
        ).raise_for_status()
        print("guidance unchanged by new notes (v{} still current).".format(
            active["version"]))
        return

    row = insert_version(cfg, result["content"].strip(), result["changelog"],
                         source_hash)
    print("guidance v{} activated: {}".format(row["version"], result["changelog"]))

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a") as f:
            f.write("## Writing guidance updated to v{}\n\n{}\n\n"
                    "Review or revert at https://joiceapp.com/review/\n\n".format(
                        row["version"], result["changelog"]))


if __name__ == "__main__":
    main()
