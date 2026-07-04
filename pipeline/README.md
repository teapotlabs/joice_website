# Joice blog pipeline

Automated blog generation for joiceapp.com. Twice a day a GitHub Actions cron
job researches a topic, writes an essay with real citations, renders the site,
and opens a pull request. **Merging the PR is the human review gate** — once it
lands on `main`, Cloudflare Pages deploys it.

```
                    ┌────────────────────────────────────────────┐
 cron (2x/day) ──▶  │ generate_post.py                           │
                    │  1. research (Claude + web search)         │
                    │  2. draft (style guide, inline citations)  │
                    │  3. polish (AI-tell hunt, plug check)      │
                    │  -> posts/<slug>.md                        │
                    └────────────────┬───────────────────────────┘
                                     ▼
                    ┌────────────────────────────────────────────┐
                    │ build_blog.py                              │
                    │  posts/*.md -> blog/<slug>/index.html,     │
                    │  blog/index.html (timeline + search),      │
                    │  posts.json, feed.xml, sitemap.xml,        │
                    │  robots.txt, llms.txt                      │
                    └────────────────┬───────────────────────────┘
                                     ▼
                       PR opened ──▶ human review ──▶ merge
                                     ▼
                          Cloudflare Pages deploys main
```

## One-time setup

1. **Repo secret** — add `ANTHROPIC_API_KEY` under Settings → Secrets and
   variables → Actions. The generator uses `claude-opus-4-8`.
2. **Allow Actions to open PRs** — Settings → Actions → General → Workflow
   permissions: check "Allow GitHub Actions to create and approve pull
   requests" (and "Read and write permissions").
3. **App Store URL** — `pipeline/config.yml` currently points the in-post app
   plug at `https://joiceapp.com/#download`. Replace `app_store_url` with the
   real App Store link when the app is live.

## Files

| file | purpose |
|---|---|
| `config.yml` | site URLs, brand blurb, content pillars, limits |
| `style_guide.md` | the writer's voice + banned AI tells + citation rules |
| `generate_post.py` | research → draft → polish; writes `posts/<slug>.md` |
| `build_blog.py` | deterministic renderer for all blog output |
| `../posts/*.md` | source of truth: one markdown file per post |
| `../blog/` | generated HTML (committed; served by Cloudflare Pages) |

## Local usage

```sh
pip install -r pipeline/requirements.txt

# generate a post (needs ANTHROPIC_API_KEY or an `ant auth login` profile)
python3 pipeline/generate_post.py                 # auto topic
python3 pipeline/generate_post.py --topic "..."   # specific topic

# rebuild the blog after editing any posts/*.md
python3 pipeline/build_blog.py
```

Posts are plain markdown with YAML frontmatter — you can also write one by
hand, drop it in `posts/`, run `build_blog.py`, and commit.

## Review checklist (for the human on the PR)

- Do the cited links actually support the claims made?
- Does it read like a person wrote it? Any AI tells the editor missed?
- Is the Joice mention tasteful (1-2 links, no hard sell)?
- Title/description sensible for search?

## Schedule

`.github/workflows/blog-generate.yml` runs at 13:07 and 22:07 UTC (~6am and
~3pm Pacific). Edit the two `cron:` lines to change cadence. You can also run
it on demand from the Actions tab (`workflow_dispatch`), optionally with a
topic override.
