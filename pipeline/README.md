# Joice blog pipeline

Automated blog generation for joiceapp.com. Twice a day a GitHub Actions cron
job researches a topic, writes an essay with real citations, and uploads it to
**Supabase as a draft**. A human reviews the draft in the Supabase Table
Editor and flips `status` to `published` — the website renders posts straight
from the database, so publishing is **instant and requires no deploy or PR**.

```
                    ┌────────────────────────────────────────────┐
 cron (2x/day) ──▶  │ generate_post.py                           │
                    │  1. research (Claude + web search)         │
                    │  2. draft (style guide, inline citations)  │
                    │  3. polish (AI-tell hunt, plug check)      │
                    │  -> INSERT into Supabase posts (draft)     │
                    └────────────────┬───────────────────────────┘
                                     ▼
              human review in Supabase Table Editor
                 (edit body_md, flip status -> published)
                                     ▼
        Cloudflare Pages Functions render /blog/* from Supabase
              on request — the post is live immediately
```

## Where things live

| piece | location |
|---|---|
| posts (content) | Supabase project "Joice Website" (`hfcykydchzwfgotnztsb`), table `public.posts` |
| website rendering | `functions/blog/[[path]].js`, `functions/sitemap.xml.js`, `functions/llms.txt.js` + shared `functions-lib/blog.js` (Cloudflare Pages Functions, deployed with the site) |
| generator | `pipeline/generate_post.py` (GitHub Actions cron, `.github/workflows/blog-generate.yml`) |
| writer voice | `pipeline/style_guide.md` |
| topic pillars + limits | `pipeline/config.yml` |

The Pages Functions read with the *publishable* key (safe to embed; row-level
security exposes only `status = 'published'` rows). The generator writes with
the *secret* key, which lives only in GitHub Actions secrets.

## One-time setup

1. **Repo secrets** — Settings → Secrets and variables → Actions:
   - `ANTHROPIC_API_KEY` — Claude API key (generator uses `claude-opus-4-8`)
   - `SUPABASE_SECRET_KEY` — from Supabase dashboard → Project Settings →
     API Keys → secret key
2. That's it. Cloudflare Pages picks up the `functions/` directory
   automatically on the next deploy of the repo.

## Review workflow (per post)

1. The Actions run finishes and its job summary links to the draft.
2. Open the [posts table](https://supabase.com/dashboard/project/hfcykydchzwfgotnztsb/editor)
   in Supabase, read `body_md`, edit anything you like.
3. Check: citations support the claims; no AI tells; the Joice plug is
   tasteful (1-2 links); title/description sensible for search.
4. Set `status` to `published`. Done — live at `/blog/<slug>/` within a
   minute (pages cache for up to 5 minutes at the edge).

To unpublish, set `status` back to `draft`. To fix a typo, just edit
`body_md` — the site re-renders on the next request.

## Local usage

```sh
pip install -r pipeline/requirements.txt

# needs ANTHROPIC_API_KEY; needs SUPABASE_SECRET_KEY unless --dry-run
python3 pipeline/generate_post.py                     # auto topic -> draft
python3 pipeline/generate_post.py --topic "..."       # specific topic
python3 pipeline/generate_post.py --dry-run out.json  # no upload, no keys
```

To test the website functions locally:

```sh
npx wrangler pages dev .    # serves static site + functions on :8788
```

## Schedule

`.github/workflows/blog-generate.yml` runs at 13:07 and 22:07 UTC (~6am and
~3pm Pacific). Edit the two `cron:` lines to change cadence, or run on demand
from the Actions tab (optionally with a topic override).
