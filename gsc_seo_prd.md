# GSC → blog pipeline integration

Plan for a weekly SEO loop: every Sunday the pipeline reads Google Search
Console, distills what's working and what isn't, and produces a plan for the
week — new topics to write and existing posts to optimize — that the daily
generation runs then execute.

## Goals

1. **Close the loop.** Today the pipeline writes into the void: topics are
   picked by the research model with no signal about what actually ranks or
   gets clicked. GSC is the ground truth for that.
2. **Optimize existing posts, not just add new ones.** Most SEO wins on a
   young blog come from improving pages already getting impressions
   (striking-distance rankings, bad CTR on good positions), not from net-new
   content.
3. **Accumulate learnings.** Each week's observations should compound into
   standing SEO guidance the writer models see — same pattern as the
   reviewer-notes → `prompt_guidance` flow that already exists.
4. **Review after publish, not before.** Posts go live without waiting for
   approval; the review console shifts from a pre-publication gate to a
   post-publication editing surface (see "Auto-publish" below).

## Non-goals (for now)

- Keyword-tool integrations (Ahrefs/Semrush) — GSC only.
- Backlink work, technical SEO audits, Core Web Vitals.
- Automatic publishing of any change to a live post.

## Access: GSC API from GitHub Actions

- **Property**: `sc-domain:joiceapp.com` (or the URL-prefix property if
  that's what's verified — confirm in the GSC UI).
- **Auth**: Google Cloud service account. One-time setup:
  1. Create a GCP project (or reuse one), enable the *Search Console API*.
  2. Create a service account, download its JSON key.
  3. In GSC → Settings → Users, add the service account email with
     *Full* permission (Restricted is enough for Search Analytics, but Full
     keeps options open for sitemaps).
  4. Store the JSON key as GitHub Actions secret `GSC_SERVICE_ACCOUNT_JSON`.
- **Client**: `google-api-python-client` + `google-auth` added to
  `pipeline/requirements.txt`. Only the `searchanalytics.query` endpoint is
  needed for v1 (sitemaps/URL-inspection later if ever).
- **Data lag**: GSC data trails by ~2 days. All windows below end at
  `today - 3 days` to avoid partial days.

## The Sunday job

New workflow `.github/workflows/blog-seo-review.yml`:

- cron: Sundays `7 12 * * 0` (~5am Pacific), an hour before the day's
  first generation run, so the planning doc is ready by ~6am and the
  week's plan exists before Sunday's first post. Generation runs twice
  daily, seven days a week (`7 13 * * *` and `7 22 * * *`).
- `workflow_dispatch` for manual runs.
- Runs new script `pipeline/seo_review.py` with `ANTHROPIC_API_KEY`,
  `SUPABASE_SECRET_KEY`, `GSC_SERVICE_ACCOUNT_JSON`.

### Step 1 — pull metrics

Three Search Analytics queries, current window = last 28 days, previous
window = the 28 days before that (for deltas):

| Pull | Dimensions | Purpose |
|---|---|---|
| Site queries | `query` | What people find us for; trends |
| Blog pages | `page` (filter `/blog/`) | Per-post clicks/impressions/CTR/position |
| Query × page | `query, page` (filter `/blog/`) | Which query each post ranks for; cannibalization |

Also join against the `posts` table (slug, title, format, published date) so
every page row maps to a post and its format.

### Step 2 — mechanical analysis (plain Python, no model)

Compute a structured findings object:

- **Striking distance**: query×page rows with avg position 4–20 and
  meaningful impressions — the classic "already ranks, could rank better"
  list.
- **CTR laggards**: rows where position is good (≤10) but CTR is well below
  what that position should earn — title/meta problems.
- **Content gaps**: site queries with impressions where no blog page ranks,
  or where only the homepage ranks — topic candidates.
- **Winners**: posts/queries with improving clicks or position vs the
  previous window — evidence of what's working (formats, angles).
- **Decay**: posts whose clicks/position dropped meaningfully vs the
  previous window.
- **Zero-traction posts**: published ≥ 6 weeks with ~no impressions.

Thresholds live in `config.yml` under a new `seo:` section so they're
tunable without code changes (min impressions, position bands, CTR-vs-
position expectations, window length).

### Step 3 — model pass: learnings + weekly plan

One model call per week (same retry wrapper `stream_message`) takes the
findings object plus context (pillars, formats, recent post list, last
week's plan and its outcomes) and returns, via a JSON schema. Because
step 2 has already done the number-crunching in Python, this is a
synthesis task — one call a week, so model cost is minor either way.
(Decided 2026-08-10: run it on **Fable 5** for the strongest weekly plan;
the job stays cheap regardless — zero web searches, mechanical analysis
in plain Python, a single model call.)

- **`learnings`** (prose): what this week's data says — which formats/topics
  earn impressions, title patterns that get clicked, patterns to stop.
  Written as durable guidance, not a data recap.
- **`plan.topics`**: up to N (default 5) new-post proposals, each with the
  target query cluster, a specific working topic, suggested format, and a
  one-line rationale tied to the data. These must fit the existing pillars.
- **`plan.optimizations`**: up to N (default 3) existing posts to improve,
  each with the slug, the diagnosis ("pos 8.4 for 'journaling prompts',
  1.9% CTR"), and concrete actions (retitle, answer X directly in the
  opening, add a section for query Y, update meta description).

### Step 4 — store the report

New table (service-role only, like `post_research`):

```sql
create table public.seo_reports (
  id           uuid primary key default gen_random_uuid(),
  created_at   timestamptz not null default now(),
  window_start date not null,
  window_end   date not null,
  raw_findings jsonb not null,   -- step-2 output, trimmed
  learnings    text not null,    -- step-3 prose
  plan         jsonb not null,   -- topics[] + optimizations[], each with a "state" field
  plan_doc     text not null,    -- rendered weekly planning doc (markdown)
  status       text not null default 'active'  -- 'active' | 'superseded'
);
```

Inserting a new report marks prior ones `superseded`. Plan items carry
`state: pending | done | skipped` so weekday runs can consume them.

**`plan_doc` — the human-readable record.** Alongside the structured
fields, the Sunday job renders a markdown planning document and stores it
in the same row, so every week's plan is preserved verbatim and can be
referred back to later. The step-3 schema gains a `plan_doc` field the
model writes directly (it's better at prose than a template is), covering:

- the week's headline numbers (clicks, impressions, avg position, deltas
  vs the prior window);
- the learnings, written to be readable standalone;
- the plan — each topic and optimization with its rationale;
- a short "how last week went" retrospective, generated by joining the
  prior report's plan items against their `state` and the resulting
  posts' early GSC numbers.

Rows are never deleted — `superseded` reports form the archive, so the
full history of weekly planning docs lives in the table and is queryable
by date.

## How the plan feeds the week

### New topics → daily generation

`generate_post.py` gains a step before topic selection: fetch the active
`seo_reports` row; if `plan.topics` has a `pending` item, use it as the
topic override (highest priority first) and PATCH its state to `done` with
the resulting post id. When the queue is empty, fall back to today's free
topic pick. With ~14 runs/week and ~5 planned topics, most runs still
free-pick — the plan steers, it doesn't take over. `--topic` on manual runs
still wins over everything.

### Optimizations → rewrite flow, without unpublishing

The existing rewrite flow flips posts to `rewrite_requested` → `draft`,
which would pull a published post off the site (anon RLS only reads
`published`). So published-post optimization needs a revision mechanism:

- Add `pending_revision jsonb` (title, meta_description, content, notes) to
  `posts`.
- Sunday job writes each optimization's brief into
  `posts.seo_notes` (new text column) and sets a new status-adjacent flag
  `optimize_requested boolean` — the post stays `published` and live.
- The hourly `blog-rewrite.yml` picks these up too:
  `process_rewrites.py` gets a second queue — posts with
  `optimize_requested = true` — and runs a *revision* pass: same writer
  model, prompt = current post + SEO notes + style guide, instructed to
  make targeted changes (not a rewrite). Output lands in
  `pending_revision`; the live post is untouched.
- The review console shows posts with a pending revision, with a diff-ish
  view (old vs new title, changed sections) and **apply** / **discard**
  actions. Apply copies the revision into the live columns and clears it —
  the post never leaves `published`. (Slug never changes; title/meta/body
  may.)

This is the largest chunk of work (console UI + RPCs + rewrite script
changes). If it needs to be phased, ship it as: Sunday job only writes
`seo_notes`, and the console shows them as a to-do list for manual action —
the revision automation comes after.

### Learnings → standing SEO guidance

Reuse the `prompt_guidance` pattern: the Sunday job folds each week's
`learnings` into a versioned standing *SEO guidance* doc (either a second
`kind` column on `prompt_guidance` or a parallel table). The active version
is appended to generation/rewrite prompts alongside the editorial guidance
in `load_effective_style_guide`. This is where "questions phrased exactly
as searched get clicks" type insights persist beyond the week they were
noticed.

## Auto-publish: drop the approval gate, keep the review console

Today nothing goes live until a draft is manually flipped to `published`.
That gate goes away: generated posts publish automatically, and the review
console at joiceapp.com/review/ becomes a **post-publication** editing
surface.

### Pipeline change

- `generate_post.py` inserts with `status='published'` when `validate()`
  passes (config flag `auto_publish: true` in `config.yml`, plus a
  `--draft` CLI override for testing). Posts that fail validation still
  land as `draft` for manual attention rather than being discarded.
- `validate()` is now the only pre-publication check, so it's worth
  tightening: word-count bounds, minimum sources with resolvable URLs, App
  Store link present, slug collision, no leftover placeholder text.
- Optional softer variant if fully-immediate feels risky at first: insert
  as `draft` with a `publish_at` timestamp a few hours out, and let the
  hourly workflow flip anything past due. That preserves a "catch it on
  your phone" window without making publication depend on review. The doc
  recommends starting with the simple immediate version — the edge cache
  already means ~5 minutes of latency, and anything bad can be edited or
  unpublished from the console within minutes.

### Review console changes

The console keeps its role as the human quality loop — it just acts on
live posts now:

- **Edit** works directly on published rows (title, body, meta,
  tags); the existing `review_*` SECURITY DEFINER RPCs are extended to
  target `status='published'`, still behind the `allowed_reviewers`
  allowlist. Edits are live within the ~5-minute cache window. Slug stays
  immutable so URLs never break.
- **Notes** keep working exactly as today and still feed
  `update_guidance.py` — the standing-guidance loop doesn't care whether
  the note was written before or after publication.
- **Request rewrite** can no longer flip a post to `rewrite_requested` →
  `draft`, because that would unpublish it. It routes through the same
  `pending_revision` mechanism the SEO optimizations use (above): the
  hourly job writes a revision alongside the live post, and the console
  gets apply/discard. One mechanism serves both human-requested and
  SEO-driven rewrites.
- **Unpublish** button (sets `status='draft'`) as the emergency brake for
  a post that shouldn't have gone out.

This dovetails with the GSC plan: `pending_revision` was already required
for optimizing published posts, so auto-publish adds no new machinery —
it just makes that mechanism the single rewrite path. It also means plan
topics hit the index the day they're written, so Search Console starts
returning signal on them a week sooner.

## Review console touchpoints (SEO)

- New "seo" screen: this week's rendered `plan_doc` plus the plan items
  with per-item state; lets the reviewer skip a planned topic or edit an
  optimization note before the week runs it.
- A history list of past weeks' planning docs (from `superseded` rows),
  so any prior Sunday's plan can be re-read from the phone.
- Pending-revision review as described above (now shared with
  human-requested rewrites).
- Both behind the existing `allowed_reviewers` RPC pattern (new
  `seo_*` SECURITY DEFINER RPCs; publishable key only).

## Phasing

0. **Phase 0 — auto-publish.** Independent of GSC and shippable first:
   tighten `validate()`, flip the insert to `published`, extend the
   console RPCs to edit live posts, add unpublish. (The revision flow can
   lag behind — worst case, "request rewrite" is briefly unavailable and
   edits are manual.)
1. **Phase 1 — read + report.** GSC auth, `seo_review.py` steps 1–4,
   Sunday workflow, `seo_reports` table. Output is a stored report; a
   simple read-only view in the console (or even just the Supabase table)
   is enough to validate the analysis quality for a few weeks.
2. **Phase 2 — topics.** `generate_post.py` consumes `plan.topics`.
   Low risk: worst case is a mediocre topic that still goes through draft
   review.
3. **Phase 3 — optimizations.** `seo_notes` + revision pass + console
   apply/discard UI.
4. **Phase 4 — standing guidance.** Versioned SEO guidance appended to
   prompts, once a few weeks of learnings show what's worth persisting.

## Cost efficiency

The pipeline's spend today is dominated by the twice-daily generation run,
not by anything this plan adds. Per post: a research pass on Opus 4.8
($5/$25 per MTok) with up to 10 web searches, then a draft pass **and** a
full polish pass on Fable 5 ($10/$50 per MTok — output is the expensive
half). At ~14 posts/week that's ~60 posts/month, most of which the review
gate never publishes. Measures, ranked by expected impact:

1. **The volume dial is the cron itself.** With auto-publish there's no
   draft backlog to gate on. (Decided 2026-08-10: stay at twice daily,
   seven days a week — the Batch API discount and GSC-targeted topics
   carry the efficiency instead. If index-coverage checks show new posts
   going unindexed, revisit cadence first.)

2. **Batch API — 50% off everything (no quality change).** The pipeline is
   a cron job; nobody is waiting on latency. The Message Batches API
   processes the same requests (web search and all other features
   included) at half price, usually completing within the hour (24h max).
   Wrap `stream_message` so each pass submits a batch-of-one and polls for
   the result inside the workflow. This halves model spend across
   research, draft, and polish with zero model or prompt changes — the
   cost is implementation complexity (submit/poll instead of stream, and
   the workflow run takes longer). Recommended second step after the
   backlog gate.

3. **Merge or demote the polish pass.** Two full Fable output passes per
   post is the priciest recurring element — polish re-emits the entire
   essay at $50/MTok output. Options, in order of aggressiveness: lower
   the polish call's `output_config.effort` to `medium` (output shrinks,
   quality usually holds for revision-type work); fold draft+polish into
   one pass (~40% off writer cost); or run polish only when `validate()`
   flags problems. A/B against the review console for two weeks — the
   human gate catches any regression before readers see it.

4. **Cheaper models for non-prose steps.** Keep Fable for reader-facing
   prose (the split exists for a reason), but two steps don't produce
   prose:
   - `update_guidance.py` distillation currently runs on `RESEARCH_MODEL`
     (Opus). It's a summarization task — Sonnet 5 handles it; it already
     skips when notes are unchanged, so savings are modest but free.
   - The new Sunday SEO call runs on Sonnet 5 from day one (above).

   The bolder trial: research on Sonnet 5 instead of Opus. Search-and-
   summarize is squarely in Sonnet's strengths, and it's ~40% cheaper
   ($2/$10 intro pricing through 2026-08-31 makes the trial cheaper
   still). Worth a two-week trial with `RESEARCH_MODEL` as an env
   override; the research brief is archived per post in `post_research`,
   so briefs from both models can be compared directly.

5. **Trim web search.** Research currently allows `max_uses: 10` searches
   per post (searches are billed per use on top of tokens). Question/
   listicle formats rarely need 10; drop the default to 6 and keep 10 only
   for `news_commentary`, which genuinely needs to hunt.

6. **Prompt caching (minor).** Draft and polish run back-to-back and share
   the style guide + research brief. Structuring both prompts with the
   shared content first and a `cache_control` breakpoint gets the polish
   call's shared input at ~0.1× price. Input is a small share of cost next
   to Fable output tokens, so this is a nice-to-have, not a project.

Suggested order: ship 1 immediately (it's a ~10-line change), then 2, then
trial 3 and 4 with the review console as the safety net. Together, 1 + 2
alone plausibly cut pipeline spend by 60–70% without touching output
quality.

## Beyond the loop: compounding SEO assets

The GSC loop optimizes what we write. These make every post worth more,
roughly in order of value-for-effort. The first three are cheap and purely
technical; the later ones are strategy.

1. **JSON-LD structured data.** (Correction: `functions/blog/[[path]].js`
   already renders `BlogPosting` + `BreadcrumbList` JSON-LD and a
   related-posts block — the earlier "missing" claim was wrong.) The real
   gap was `dateModified`: now added to the JSON-LD and as
   `article:modified_time`, driven by a DB trigger that bumps
   `updated_at` only when reader-visible content changes.

2. **Internal linking — the biggest content-side gap.** Posts link out to
   sources and to the App Store, but not to each other, so no authority
   flows between pages and crawl discovery depends entirely on the index
   page and sitemap. Three layers:
   - *At generation:* pass the writer the published posts list (title,
     slug, description — it's already fetched for topic dedupe) and have
     the style guide require 1–3 natural inline links to related posts.
   - *At render:* a "related posts" block on each post page, matched by
     shared tags — pure SSR, no model involved.
   - *Retroactively:* the Sunday optimizer's `pending_revision` flow can
     add links from older posts to newer relevant ones — new posts
     otherwise start with zero internal links pointing at them.

3. **Freshness signals once editing goes live.** With auto-publish +
   console edits + revisions, pages will change after publication — make
   that visible: `sitemap.xml` `lastmod` should use
   `greatest(published_at, updated_at)`, and the `BlogPosting`
   `dateModified` should update when a revision or edit lands. Updated
   content re-earns crawls; silent updates don't.

4. **Index-status check in the Sunday job.** Search Analytics only shows
   pages that already get impressions — it can't see a post Google never
   indexed. The URL Inspection API (generous daily quota, and we only need
   ~10 URLs/week) lets the Sunday job verify that recently published posts
   are actually indexed and flag ones that aren't in the weekly plan doc.
   Cheap add: also submit new URLs to IndexNow (Bing/DuckDuckGo) at
   publish time — one HTTP call in the publish path.

5. **Topic clusters over scattershot.** Instruct the Sunday planner to
   build *clusters*: several posts targeting related queries within one
   pillar, densely interlinked, eventually anchored by a comprehensive hub
   page per pillar (e.g. "the complete guide to voice journaling"). Search
   engines reward demonstrated depth on a topic far more than breadth
   across many. This is a planning-prompt change, not new machinery.

6. **AI-search (AEO) posture.** `llms.txt` already exists — good, as a
   growing share of discovery runs through LLM assistants. Reinforce it:
   answer-first structure (the question format already does this — the
   planner should favor it for gap-filling), and a one-paragraph
   plainly-stated summary near the top of each post that an LLM can lift
   and cite. Citations from AI assistants are referral traffic GSC won't
   show, so add a UTM-free referrer check to the weekly numbers if it
   becomes material.

7. **`seo_events` change log for attribution.** Log every SEO-relevant
   change (publish, title change, revision applied, unpublish) with a
   timestamp into a small table. The Sunday retrospective can then line up
   ranking/CTR movements against what changed and when — without it,
   "did that retitle work?" is guesswork. Ten lines of code in the paths
   that already write to `posts`.

## Open questions

- **Data volume**: the blog is young; early weeks may have too few
  impressions for meaningful striking-distance/CTR analysis. The model
  pass should be told the sample sizes and instructed to say "not enough
  data" rather than invent patterns — and the plan can be mostly
  content-gap topics until traffic grows.
- **Which GSC property** is verified (domain vs URL-prefix) — determines
  the `siteUrl` parameter and whether www/apex are unified.
- **Cannibalization handling**: when two posts rank for the same query, is
  the fix a merge (needs redirects — new machinery) or differentiation
  (just an optimization note)? Start with differentiation only.
- ~~Whether `plan.topics` items should force their suggested format~~ —
  decided: forced. The format suggestion is part of the SEO rationale.
