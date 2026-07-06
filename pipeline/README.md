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

**The review console at [joiceapp.com/review/](https://joiceapp.com/review/)**
is the primary tool — mobile-friendly, gated by Supabase Auth (Google
sign-in or email code) against the `private.allowed_reviewers` email
allowlist. From there you can:

- read every draft, edit title/description/body inline, and **approve &
  publish** (live at `/blog/<slug>/` within ~5 minutes)
- **save notes** on any post — notes steer that post's rewrite, are injected
  raw into upcoming prompts (`reviewer_feedback()`), and are **distilled into
  versioned standing guidance** (`update_guidance.py`, runs before each
  generation) that permanently amends the style guide in every prompt
- manage that guidance from the console's "writing guidance" screen: edit it
  (saves a new version) or revert to any previous version
- **request a rewrite** — flags the post `rewrite_requested`; the hourly
  `blog-rewrite.yml` workflow rewrites it against your notes
  (`process_rewrites.py`) and returns it to the drafts queue
- unpublish anything published

The console talks to SECURITY DEFINER Postgres RPCs (`review_*`,
`guidance_*`) that verify the caller's email against the allowlist, using
only the publishable key — no server or extra secrets. Add reviewers with
`insert into private.allowed_reviewers (email) values ('...')`. Google
sign-in additionally needs a one-time OAuth client setup in the Supabase
dashboard (Auth → Providers → Google); the email-code flow works out of the
box. The Supabase Table Editor still works as a fallback.

Review checklist: citations support the claims; no AI tells; the Joice plug
is tasteful (1-2 links); title/description sensible for search.

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
