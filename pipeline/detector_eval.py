#!/usr/bin/env python3
"""Feasibility experiment: score blog posts against an AI-text detector.

Answers one question before we wire a detector gate into the pipeline: can a
prompt-level "humanize" rewrite pass move the detector's AI score meaningfully,
or is Claude-written prose pinned near 1.0 no matter what? (See the AuthorMist
paper, arXiv:2503.08716.) Pangram is the stricter, paraphrase-robust detector;
GPTZero sits closer to human perception and is the more realistic gate target.

Modes:
  --detector D         pangram (default) or gptzero
  --baseline           score the most recent published posts, no rewriting
  --rewrite-test N     also run humanize rewrite passes on the N highest-
                       scoring posts and report the score trajectory

Environment:
  PANGRAM_API_KEY / GPTZERO_API_KEY   key for the chosen --detector
  ANTHROPIC_API_KEY                   Claude API key (rewrite mode only)

Reads published posts with the publishable Supabase key (anon RLS), so no
Supabase secret is needed. Nothing is written back to the database.
"""

import argparse
import json
import os
import re
import sys
import time

import requests

from generate_post import (
    WRITER_MODEL, collect_text, load_config, load_style_guide, stream_message,
)

PANGRAM_BASE = "https://text.external-api.pangram.com"
GPTZERO_BASE = "https://api.gptzero.me/v2"
DETECTORS = ("pangram", "gptzero")

# Publishable key — public by design (also shipped in functions-lib/blog.js);
# RLS limits it to reading published rows.
SUPABASE_PUBLISHABLE_KEY = "sb_publishable_DWplZMwp3dt_50oX6aCoXA_fJ0H0rT2"

REWRITE_SCHEMA = {
    "type": "object",
    "properties": {
        "body_markdown": {"type": "string",
                          "description": "The rewritten piece, full markdown body"},
    },
    "required": ["body_markdown"],
    "additionalProperties": False,
}


def fetch_published(cfg, limit):
    r = requests.get(
        cfg["supabase_url"] + "/rest/v1/posts",
        params={"select": "slug,title,body_md", "status": "eq.published",
                "order": "created_at.desc", "limit": str(limit)},
        headers={"apikey": SUPABASE_PUBLISHABLE_KEY,
                 "Authorization": "Bearer " + SUPABASE_PUBLISHABLE_KEY},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def markdown_to_text(md):
    """Approximate the prose a reader (and detector) actually sees."""
    text = re.sub(r"```.*?```", "", md, flags=re.S)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)   # [text](url) -> text
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.M)
    text = re.sub(r"[*_]{1,3}([^*_]+)[*_]{1,3}", r"\1", text)
    text = re.sub(r"^>\s?", "", text, flags=re.M)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


# Each scorer returns a normalized dict so the rest of the file is
# detector-agnostic:
#   score    float  probability the text is AI (0-1); the gate metric
#   verdict  str    detector's own short label
#   detail   str    one-line human summary for logs
#   flagged  list   worst passages, each {text, score, label}, worst first
#   raw      dict   the full API response, archived to the results file

def pangram_score(text, poll_seconds=3, timeout=180):
    """Submit text to Pangram's async API; return the normalized result."""
    headers = {"x-api-key": os.environ["PANGRAM_API_KEY"],
               "Content-Type": "application/json"}
    r = requests.post(PANGRAM_BASE + "/task", headers=headers,
                      json={"text": text}, timeout=30)
    r.raise_for_status()
    task_id = r.json()["task_id"]

    deadline = time.time() + timeout
    while time.time() < deadline:
        r = requests.get("{}/task/{}".format(PANGRAM_BASE, task_id),
                         headers=headers, timeout=30)
        r.raise_for_status()
        result = r.json()
        stage = result.get("stage")
        if stage == "STAGE_SUCCESS":
            break
        if stage == "STAGE_FAILED":
            raise RuntimeError("Pangram task {} failed: {}".format(task_id, result))
        time.sleep(poll_seconds)
    else:
        raise RuntimeError("Pangram task {} timed out".format(task_id))

    windows = sorted(result.get("windows") or [],
                     key=lambda w: w.get("ai_assistance_score", 0), reverse=True)
    return {
        "score": result.get("fraction_ai", -1),
        "verdict": result.get("prediction_short", "?"),
        "detail": "fraction_ai={:.2f} ai_assisted={:.2f} human={:.2f} verdict={}"
                  .format(result.get("fraction_ai", -1),
                          result.get("fraction_ai_assisted", -1),
                          result.get("fraction_human", -1),
                          result.get("prediction_short", "?")),
        "flagged": [{"text": w.get("text", ""),
                     "score": w.get("ai_assistance_score", 0),
                     "label": w.get("label", "?")}
                    for w in windows[:5]],
        "raw": result,
    }


def gptzero_score(text):
    """Score text with GPTZero's synchronous API; return the normalized result.

    completely_generated_prob is the document-level AI probability — the field
    that maps to the '<50% chance of AI' gate."""
    r = requests.post(
        GPTZERO_BASE + "/predict/text",
        headers={"x-api-key": os.environ["GPTZERO_API_KEY"],
                 "Content-Type": "application/json"},
        json={"document": text}, timeout=60,
    )
    r.raise_for_status()
    doc = r.json()["documents"][0]
    sentences = sorted(doc.get("sentences") or [],
                       key=lambda s: s.get("generated_prob", 0), reverse=True)
    return {
        "score": doc.get("completely_generated_prob", -1),
        "verdict": doc.get("predicted_class", "?"),
        "detail": "completely_generated_prob={:.2f} predicted={} confidence={}"
                  .format(doc.get("completely_generated_prob", -1),
                          doc.get("predicted_class", "?"),
                          doc.get("confidence_category", "?")),
        "flagged": [{"text": s.get("sentence", ""),
                     "score": s.get("generated_prob", 0),
                     "label": "ai" if s.get("highlight_sentence_for_ai") else ""}
                    for s in sentences[:5]],
        "raw": doc,
    }


SCORERS = {"pangram": pangram_score, "gptzero": gptzero_score}


def humanize(client, style_guide, body_md, result):
    flagged = "\n\n".join(
        '- (AI score {:.2f}{}) "{}"'.format(
            w["score"], " " + w["label"] if w["label"] else "",
            w["text"][:400])
        for w in result["flagged"])
    prompt = (
        "You are a line editor. The piece below was flagged by an AI-text "
        "detector; the flagged passages are listed after it, worst first.\n\n"
        "STYLE GUIDE (the piece must keep this voice):\n{style}\n\n"
        "PIECE (markdown):\n{body}\n\n"
        "DETECTOR-FLAGGED PASSAGES:\n{flagged}\n\n"
        "Rewrite the piece so it reads like one specific person wrote it in "
        "one sitting. Focus hardest on the flagged passages, but treat the "
        "whole text. Concretely:\n"
        "- vary sentence length aggressively — fragments, short punches, the "
        "occasional long winding sentence\n"
        "- break parallel structures; real writers don't produce three "
        "matching clauses in a row\n"
        "- replace generic phrasing with specific, opinionated wording; add "
        "small asides or hedges where a person naturally would\n"
        "- keep EVERY factual claim, statistic, and inline markdown link "
        "exactly as cited; do not add or drop sources\n"
        "- keep the overall length within about 10% of the original\n"
        "- keep the markdown structure (headings, lists) unless flattening a "
        "list into prose genuinely reads more human"
    ).format(style=style_guide, body=body_md, flagged=flagged)

    response = stream_message(
        client,
        model=WRITER_MODEL,
        max_tokens=32000,
        thinking={"type": "adaptive"},
        output_config={"format": {"type": "json_schema", "schema": REWRITE_SCHEMA}},
        messages=[{"role": "user", "content": prompt}],
    )
    return json.loads(collect_text(response))["body_markdown"]


def links_preserved(before_md, after_md):
    before = set(re.findall(r"\]\((https?://[^)\s]+)\)", before_md))
    after = set(re.findall(r"\]\((https?://[^)\s]+)\)", after_md))
    return before <= after, before - after


def summarize(lines):
    print("\n".join(lines), flush=True)
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a") as f:
            f.write("\n".join(lines) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", action="store_true",
                        help="Score recent published posts")
    parser.add_argument("--rewrite-test", type=int, metavar="N", default=0,
                        help="Humanize-rewrite the N highest-scoring posts")
    parser.add_argument("--limit", type=int, default=12,
                        help="How many recent published posts to score")
    parser.add_argument("--passes", type=int, default=2,
                        help="Max humanize passes per post in rewrite mode")
    parser.add_argument("--detector", choices=DETECTORS, default="pangram",
                        help="Which detector API to score against")
    parser.add_argument("--out", default="detector_eval_results.json",
                        help="Write full results (incl. rewrites) to this file")
    args = parser.parse_args()

    scorer = SCORERS[args.detector]
    key_env = args.detector.upper() + "_API_KEY"
    if not os.environ.get(key_env):
        sys.exit("{} is not set".format(key_env))
    if not (args.baseline or args.rewrite_test):
        sys.exit("nothing to do: pass --baseline and/or --rewrite-test N")

    cfg = load_config()
    posts = fetch_published(cfg, args.limit)
    print("scoring {} published posts against {}...".format(
        len(posts), args.detector), flush=True)

    scored = []
    for p in posts:
        result = scorer(markdown_to_text(p["body_md"]))
        scored.append({"slug": p["slug"], "title": p["title"],
                       "body_md": p["body_md"], "result": result})
        print("  {}: {}".format(p["slug"], result["detail"]), flush=True)

    fractions = [s["result"]["score"] for s in scored]
    lines = [
        "## {} baseline ({} posts)".format(args.detector, len(scored)),
        "",
        "| post | AI score | verdict |",
        "|---|---|---|",
    ]
    for s in scored:
        lines.append("| {} | {:.2f} | {} |".format(
            s["slug"], s["result"]["score"], s["result"]["verdict"]))
    lines += ["", "mean AI score: **{:.2f}**, posts under 0.50: **{}/{}**"
              .format(sum(fractions) / len(fractions),
                      sum(1 for f in fractions if f < 0.5), len(fractions))]
    summarize(lines)

    output = {"detector": args.detector,
              "baseline": [{"slug": s["slug"], "result": s["result"]}
                           for s in scored]}

    if args.rewrite_test:
        import anthropic
        client = anthropic.Anthropic()
        style_guide = load_style_guide()
        targets = sorted(scored, key=lambda s: s["result"]["score"],
                         reverse=True)[:args.rewrite_test]

        lines = ["", "## Humanize rewrite trajectories", ""]
        output["rewrites"] = []
        for s in targets:
            body, result = s["body_md"], s["result"]
            trajectory = [result["score"]]
            print("\nrewrite-testing {} (start {})".format(
                s["slug"], result["detail"]), flush=True)
            versions = []
            for i in range(args.passes):
                body = humanize(client, style_guide, body, result)
                ok, dropped = links_preserved(s["body_md"], body)
                result = scorer(markdown_to_text(body))
                trajectory.append(result["score"])
                versions.append({"body_md": body, "result": result,
                                 "links_preserved": ok,
                                 "dropped_links": sorted(dropped)})
                print("  pass {}: {}{}".format(
                    i + 1, result["detail"],
                    "" if ok else "  [WARNING: dropped links: {}]".format(
                        ", ".join(sorted(dropped)))), flush=True)
                if result["score"] < 0.5:
                    break
            lines.append("- `{}`: {}".format(
                s["slug"], " → ".join("{:.2f}".format(f) for f in trajectory)))
            output["rewrites"].append({"slug": s["slug"],
                                       "trajectory": trajectory,
                                       "versions": versions})
        summarize(lines)

    with open(args.out, "w") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)
    print("\nfull results written to {}".format(args.out), flush=True)


if __name__ == "__main__":
    main()
