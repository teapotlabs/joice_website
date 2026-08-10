#!/usr/bin/env python3
"""Weekly SEO review: read Google Search Console, plan the week.

Runs Sunday mornings (.github/workflows/blog-seo-review.yml). Steps:
  1. Pull Search Analytics for the last 28 full days (ending 3 days ago —
     GSC data lags) plus the 28 days before that for deltas: site queries,
     blog pages, and query x page.
  2. Mechanical analysis in plain Python: striking-distance rankings, CTR
     laggards, content gaps, winners, decay, zero-traction posts, plus a
     URL-inspection check that recently published posts are indexed.
  3. One Sonnet call distills learnings and writes the week's plan — up to
     N new-post topics (clustered within pillars, each with a forced
     format and target queries) and up to M optimizations of existing
     posts — plus a human-readable plan_doc, which takes into account the
     prior week's plan and the one-line topic rationales of the articles
     actually written.
  4. Store everything in seo_reports (prior rows become the archive),
     queue the optimizations as revision requests on the posts, and put
     the plan_doc in the job summary.

Weekday generation runs (generate_post.py) consume plan topics first-come
until the list is empty, then fall back to free topic picking.

Environment:
  ANTHROPIC_API_KEY         Claude API key
  SUPABASE_SECRET_KEY       Supabase secret (service) key — write access
  GSC_SERVICE_ACCOUNT_JSON  Google service account key with access to the
                            Search Console property
"""

import datetime as dt
import json
import os
import sys

import anthropic
import requests

from generate_post import (
    load_config, log_seo_event, stream_message, supabase_headers,
)
from gsc import GscClient

PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "learnings": {
            "type": "string",
            "description": "Durable prose learnings from this week's data — "
                           "what earns impressions and clicks, what to stop. "
                           "Written to be readable standalone.",
        },
        "topics": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "topic": {"type": "string",
                              "description": "Specific working topic for one post"},
                    "format": {"type": "string",
                               "description": "Format key: essay, listicle, "
                                              "news_commentary, question, or myth_busting"},
                    "target_queries": {"type": "array", "items": {"type": "string"}},
                    "rationale": {"type": "string",
                                  "description": "One sentence tying this topic to the data"},
                },
                "required": ["topic", "format", "target_queries", "rationale"],
                "additionalProperties": False,
            },
        },
        "optimizations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "slug": {"type": "string"},
                    "diagnosis": {"type": "string",
                                  "description": "What the data says is wrong, with numbers"},
                    "actions": {"type": "array", "items": {"type": "string"},
                                "description": "Concrete edits: retitle, answer X in the "
                                               "opening, add a section for query Y, ..."},
                },
                "required": ["slug", "diagnosis", "actions"],
                "additionalProperties": False,
            },
        },
        "plan_doc": {
            "type": "string",
            "description": "The full weekly planning document in markdown: headline "
                           "numbers with deltas, how last week's plan went, learnings, "
                           "and the plan with rationales. Written for a human to read "
                           "on a phone and refer back to later.",
        },
    },
    "required": ["learnings", "topics", "optimizations", "plan_doc"],
    "additionalProperties": False,
}

# Rough expected CTR by average position, for flagging title/meta laggards.
EXPECTED_CTR = [(1, 0.28), (2, 0.15), (3, 0.10), (5, 0.06), (10, 0.025)]


def expected_ctr(position):
    for cutoff, ctr in EXPECTED_CTR:
        if position <= cutoff:
            return ctr
    return 0.01


# ------------------------------------------------------------- data pulls

def windows(cfg):
    days = cfg["seo"]["window_days"]
    end = dt.date.today() - dt.timedelta(days=3)
    start = end - dt.timedelta(days=days - 1)
    prev_end = start - dt.timedelta(days=1)
    prev_start = prev_end - dt.timedelta(days=days - 1)
    return (start.isoformat(), end.isoformat(),
            prev_start.isoformat(), prev_end.isoformat())


def pull_gsc(cfg):
    gsc = GscClient(cfg["seo"]["site"])
    start, end, prev_start, prev_end = windows(cfg)

    def pulls(s, e):
        return {
            "queries": gsc.query(s, e, ["query"]),
            "pages": gsc.query(s, e, ["page"], page_filter="/blog/"),
            "query_pages": gsc.query(s, e, ["query", "page"],
                                     page_filter="/blog/"),
        }

    return {
        "window": {"start": start, "end": end},
        "prev_window": {"start": prev_start, "end": prev_end},
        "current": pulls(start, end),
        "previous": pulls(prev_start, prev_end),
    }, gsc


def fetch_posts(cfg):
    r = requests.get(
        cfg["supabase_url"] + "/rest/v1/posts",
        params={"select": "id,slug,title,format,status,published_at,"
                          "topic_rationale,revision_requested,pending_revision",
                "order": "published_at.desc.nullslast"},
        headers=supabase_headers(cfg),
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def fetch_prior_report(cfg):
    r = requests.get(
        cfg["supabase_url"] + "/rest/v1/seo_reports",
        params={"select": "id,created_at,learnings,plan,plan_doc",
                "status": "eq.active", "order": "created_at.desc", "limit": "1"},
        headers=supabase_headers(cfg),
        timeout=30,
    )
    r.raise_for_status()
    rows = r.json()
    return rows[0] if rows else None


def fetch_recent_events(cfg, days=8):
    since = (dt.datetime.now(dt.timezone.utc)
             - dt.timedelta(days=days)).isoformat()
    r = requests.get(
        cfg["supabase_url"] + "/rest/v1/seo_events",
        params={"select": "created_at,slug,event,detail",
                "created_at": "gte." + since, "order": "created_at.asc"},
        headers=supabase_headers(cfg),
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


# -------------------------------------------------------- mechanical pass

def slug_of(page_url):
    parts = page_url.rstrip("/").split("/blog/")
    return parts[1].strip("/") if len(parts) == 2 and parts[1] else None


def analyze(cfg, data, posts, gsc):
    seo = cfg["seo"]
    min_imp = seo["min_impressions"]
    lo, hi = seo["striking_distance"]

    cur, prev = data["current"], data["previous"]
    prev_pages = {r["keys"][0]: r for r in prev["pages"]}
    blog_page_slugs = {slug_of(r["keys"][0]) for r in cur["pages"]}

    def row(r, keys):
        out = dict(zip(keys, r["keys"]))
        out.update(clicks=r["clicks"], impressions=r["impressions"],
                   ctr=round(r["ctr"], 4), position=round(r["position"], 1))
        return out

    striking = [row(r, ["query", "page"]) for r in cur["query_pages"]
                if r["impressions"] >= min_imp and lo <= r["position"] <= hi]
    striking.sort(key=lambda x: -x["impressions"])

    laggards = [dict(row(r, ["query", "page"]),
                     expected_ctr=expected_ctr(r["position"]))
                for r in cur["query_pages"]
                if r["impressions"] >= min_imp and r["position"] <= 10
                and r["ctr"] < 0.5 * expected_ctr(r["position"])]
    laggards.sort(key=lambda x: -x["impressions"])

    ranked_queries = {r["keys"][0] for r in cur["query_pages"]}
    gaps = [row(r, ["query"]) for r in cur["queries"]
            if r["impressions"] >= min_imp and r["keys"][0] not in ranked_queries]
    gaps.sort(key=lambda x: -x["impressions"])

    movement = []
    for r in cur["pages"]:
        page = r["keys"][0]
        p = prev_pages.get(page)
        movement.append({
            "page": page, "clicks": r["clicks"],
            "impressions": r["impressions"], "position": round(r["position"], 1),
            "prev_clicks": p["clicks"] if p else 0,
            "prev_position": round(p["position"], 1) if p else None,
        })
    winners = sorted([m for m in movement if m["clicks"] > m["prev_clicks"]],
                     key=lambda m: m["prev_clicks"] - m["clicks"])[:10]
    decays = sorted([m for m in movement if m["clicks"] < m["prev_clicks"]],
                    key=lambda m: m["clicks"] - m["prev_clicks"])[:10]

    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(
        weeks=seo["zero_traction_weeks"])
    zero_traction = [
        p["slug"] for p in posts
        if p["status"] == "published" and p.get("published_at")
        and dt.datetime.fromisoformat(p["published_at"].replace("Z", "+00:00")) < cutoff
        and p["slug"] not in blog_page_slugs]

    # Index-status check on the newest posts (small N; generous API quota).
    recent = [p for p in posts if p["status"] == "published"
              and p.get("published_at")][:10]
    index_status = []
    for p in recent:
        url = "{}/blog/{}/".format(cfg["site_url"], p["slug"])
        try:
            indexed, coverage = gsc.inspect(url)
            index_status.append({"slug": p["slug"], "indexed": indexed,
                                 "coverage": coverage})
        except requests.RequestException as e:
            index_status.append({"slug": p["slug"], "indexed": None,
                                 "coverage": "inspection failed: {}".format(e)})

    totals = {
        "clicks": sum(r["clicks"] for r in cur["pages"]),
        "impressions": sum(r["impressions"] for r in cur["pages"]),
        "prev_clicks": sum(r["clicks"] for r in prev["pages"]),
        "prev_impressions": sum(r["impressions"] for r in prev["pages"]),
        "site_queries": len(cur["queries"]),
    }

    return {
        "totals": totals,
        "striking_distance": striking[:20],
        "ctr_laggards": laggards[:15],
        "content_gaps": gaps[:25],
        "winners": winners,
        "decays": decays,
        "zero_traction_slugs": zero_traction[:15],
        "index_status": index_status,
        "top_queries": [row(r, ["query"]) for r in
                        sorted(cur["queries"], key=lambda r: -r["clicks"])[:20]],
    }


# ----------------------------------------------------------- model pass

def last_week_context(cfg, prior, posts, events):
    """What last week's plan said, and what actually got written."""
    week_ago = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=8)
    written = []
    for p in posts:
        ts = p.get("published_at")
        if not ts:
            continue
        if dt.datetime.fromisoformat(ts.replace("Z", "+00:00")) >= week_ago:
            written.append("- {} (/blog/{}/, format {}): {}".format(
                p["title"], p["slug"], p.get("format") or "?",
                p.get("topic_rationale") or "(no rationale recorded)"))
    parts = []
    if prior:
        parts.append("LAST WEEK'S PLANNING DOC:\n" + (prior.get("plan_doc") or "(none)"))
        parts.append("LAST WEEK'S PLAN ITEM STATES:\n"
                     + json.dumps(prior.get("plan"), indent=2))
    parts.append("ARTICLES WRITTEN THIS PAST WEEK (with the one-line reason "
                 "each topic was chosen):\n" + ("\n".join(written) or "- (none)"))
    if events:
        parts.append("SEO-RELEVANT CHANGES THIS PAST WEEK (seo_events log):\n"
                     + "\n".join("- {} {} {}{}".format(
                         e["created_at"][:10], e["event"], e.get("slug") or "",
                         " — " + e["detail"] if e.get("detail") else "")
                         for e in events))
    return "\n\n".join(parts)


def run_planner(client, cfg, findings, context, published_titles):
    seo = cfg["seo"]
    pillars = "\n".join("- " + p for p in cfg["pillars"])
    formats = ", ".join(f["key"] for f in cfg.get("formats") or [])
    prompt = (
        "You are the SEO planner for the blog of {site} ({blurb}).\n\n"
        "Below: this week's Google Search Console findings (mechanically "
        "computed), last week's plan and what was actually written, and the "
        "published-post list.\n\n"
        "GSC FINDINGS:\n{findings}\n\n"
        "{context}\n\n"
        "PUBLISHED POSTS:\n{published}\n\n"
        "CONTENT PILLARS:\n{pillars}\n\n"
        "Produce this week's plan:\n"
        "- learnings: durable observations, not a data recap. If sample sizes "
        "are too small to support a pattern, say 'not enough data yet' rather "
        "than inventing one.\n"
        "- topics: up to {max_topics} new-post proposals. BUILD CLUSTERS: "
        "prefer several topics targeting related queries within one pillar — "
        "densely interlinkable — over scattershot coverage; name the cluster in "
        "each rationale. Every topic must fit a pillar and target queries real "
        "people type (favor the content_gaps and striking_distance data). Each "
        "gets a format from: {formats}. The format is part of the strategy — a "
        "question query gets a question post titled as the question.\n"
        "- optimizations: up to {max_opts} existing posts to improve, chosen "
        "from the data (striking distance, CTR laggards, decay). diagnosis "
        "quotes the numbers; actions are concrete edits an editor can make.\n"
        "- plan_doc: the weekly planning document in markdown, readable "
        "standalone on a phone. Structure: headline numbers vs the prior "
        "window; how last week's plan actually went (use the plan item states "
        "and the one-line article rationales); learnings; this week's plan "
        "with rationales; anything not yet indexed that should be. Be honest "
        "about small numbers."
    ).format(site=cfg["site_name"], blurb=cfg["brand_blurb"].strip(),
             findings=json.dumps(findings, indent=2),
             context=context,
             published="\n".join("- " + t for t in published_titles) or "- (none)",
             pillars=pillars, formats=formats,
             max_topics=seo["max_topics"], max_opts=seo["max_optimizations"])

    response = stream_message(
        client,
        model=seo["model"],
        max_tokens=16000,
        thinking={"type": "adaptive"},
        output_config={"format": {"type": "json_schema", "schema": PLAN_SCHEMA}},
        messages=[{"role": "user", "content": prompt}],
    )
    return json.loads("\n".join(
        b.text for b in response.content if b.type == "text"))


# ------------------------------------------------------------- persistence

def store_report(cfg, data, findings, result):
    for t in result["topics"]:
        t["state"] = "pending"
    for o in result["optimizations"]:
        o["state"] = "pending"
    requests.patch(
        cfg["supabase_url"] + "/rest/v1/seo_reports",
        params={"status": "eq.active"},
        headers=supabase_headers(cfg),
        json={"status": "superseded"},
        timeout=30,
    ).raise_for_status()
    r = requests.post(
        cfg["supabase_url"] + "/rest/v1/seo_reports",
        headers={**supabase_headers(cfg), "Prefer": "return=representation"},
        json={
            "window_start": data["window"]["start"],
            "window_end": data["window"]["end"],
            "raw_findings": findings,
            "learnings": result["learnings"],
            "plan": {"topics": result["topics"],
                     "optimizations": result["optimizations"]},
            "plan_doc": result["plan_doc"],
            "status": "active",
        },
        timeout=30,
    )
    r.raise_for_status()
    return r.json()[0]


def queue_optimizations(cfg, result, posts):
    by_slug = {p["slug"]: p for p in posts}
    for opt in result["optimizations"]:
        post = by_slug.get(opt["slug"])
        if not post or post["status"] != "published":
            print("skipping optimization for unknown/unpublished slug {!r}"
                  .format(opt["slug"]), file=sys.stderr)
            continue
        if post.get("revision_requested") or post.get("pending_revision"):
            print("skipping {} — revision already in flight".format(opt["slug"]))
            continue
        notes = "SEO optimization. Diagnosis: {} Actions: {}".format(
            opt["diagnosis"], " ".join("({}) {}".format(i + 1, a)
                                       for i, a in enumerate(opt["actions"])))
        requests.patch(
            cfg["supabase_url"] + "/rest/v1/posts",
            params={"id": "eq." + post["id"]},
            headers=supabase_headers(cfg),
            json={"revision_requested": True, "revision_notes": notes},
            timeout=30,
        ).raise_for_status()
        log_seo_event(cfg, post["id"], post["slug"], "revision_requested",
                      detail="SEO: " + opt["diagnosis"])
        print("queued optimization: {}".format(opt["slug"]))


def main():
    cfg = load_config()
    client = anthropic.Anthropic()

    print("[1/4] pulling Search Console data...", flush=True)
    data, gsc = pull_gsc(cfg)
    posts = fetch_posts(cfg)

    print("[2/4] analyzing...", flush=True)
    findings = analyze(cfg, data, posts, gsc)
    print(json.dumps(findings["totals"], indent=2), flush=True)

    print("[3/4] planning ({})...".format(cfg["seo"]["model"]), flush=True)
    prior = fetch_prior_report(cfg)
    context = last_week_context(cfg, prior, posts, fetch_recent_events(cfg))
    published_titles = ["{} [/blog/{}/]".format(p["title"], p["slug"])
                        for p in posts if p["status"] == "published"]
    result = run_planner(client, cfg, findings, context, published_titles)

    print("[4/4] storing report + queueing optimizations...", flush=True)
    report = store_report(cfg, data, findings, result)
    queue_optimizations(cfg, result, posts)

    print("plan: {} topic(s), {} optimization(s) — report {}".format(
        len(result["topics"]), len(result["optimizations"]), report["id"]))

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a") as f:
            f.write(result["plan_doc"] + "\n")


if __name__ == "__main__":
    main()
