// Shared library for the blog's Cloudflare Pages Functions.
// Posts live in Supabase; these functions render them to HTML at request
// time, so publishing a row makes it live on the site instantly.
//
// The Supabase key below is the *publishable* key — safe to ship publicly.
// Row-level security only exposes rows with status = 'published'.

export const SITE = "https://joiceapp.com";
export const SUPABASE_URL = "https://hfcykydchzwfgotnztsb.supabase.co";
export const SUPABASE_KEY = "sb_publishable_DWplZMwp3dt_50oX6aCoXA_fJ0H0rT2";
export const APP_STORE_URL = "https://apps.apple.com/us/app/joice-journal-by-conversation/id6759027458";
export const BLOG_TITLE = "the joice journal";
export const BLOG_DESC = "Essays on journaling, mental health, and finding your way — from the makers of Joice, the voice journal that talks back.";
export const BRAND_BLURB = "Joice is an iOS voice-journaling app. You talk out loud about whatever is on your mind and an AI companion listens like a friend — asking gentle follow-up questions, never giving unsolicited advice. Entries are transcribed and searchable, recurring life themes are surfaced over time, and each week Joice writes you a warm reflection on what you talked about. It is private and encrypted, and aimed at people who find blank pages hard but talking easy.";

const LIST_FIELDS = "slug,title,description,tags,sources,body_md,published_at,updated_at";

async function supabaseGet(path) {
  const res = await fetch(`${SUPABASE_URL}/rest/v1/${path}`, {
    headers: {
      apikey: SUPABASE_KEY,
      authorization: `Bearer ${SUPABASE_KEY}`,
      accept: "application/json",
    },
  });
  if (!res.ok) throw new Error(`supabase ${res.status}`);
  return res.json();
}

export function fetchPosts() {
  return supabaseGet(
    `posts?select=${LIST_FIELDS}&status=eq.published&order=published_at.desc`);
}

export function validSlug(slug) {
  return /^[a-z0-9]+(-[a-z0-9]+)*$/.test(slug);
}

// ---------------------------------------------------------------- utilities

export function esc(s) {
  return String(s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

export function isoDate(ts) {
  return String(ts).slice(0, 10);
}

// Freshness signal: last content edit if later than publication.
export function lastModified(p) {
  const pub = isoDate(p.published_at);
  const upd = p.updated_at ? isoDate(p.updated_at) : pub;
  return upd > pub ? upd : pub;
}

const MONTHS = ["january", "february", "march", "april", "may", "june",
  "july", "august", "september", "october", "november", "december"];

export function prettyDate(ts) {
  const [y, m, d] = isoDate(ts).split("-").map(Number);
  return `${MONTHS[m - 1]} ${d}, ${y}`;
}

export function monthLabel(ts) {
  const [y, m] = isoDate(ts).split("-").map(Number);
  return `${MONTHS[m - 1]} ${y}`;
}

export function readingMinutes(md) {
  const words = (md.match(/\w+/g) || []).length;
  return Math.max(1, Math.round(words / 220));
}

// ------------------------------------------------------- markdown rendering
// Minimal renderer covering the subset the pipeline produces: paragraphs,
// ##/### headings, links, bold/italic, inline code, blockquotes, lists, hr.
// Input is escaped first, so raw HTML in a post is displayed, not executed.

function inline(text) {
  return text
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g,
      '<a href="$2" rel="noopener">$1</a>')
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/\*([^*\n]+)\*/g, "<em>$1</em>");
}

export function mdToHtml(md) {
  const blocks = md.replace(/\r\n/g, "\n").trim().split(/\n{2,}/);
  const out = [];
  for (const raw of blocks) {
    const block = esc(raw.trim());
    if (!block) continue;
    if (/^###\s+/.test(block)) {
      out.push(`<h3>${inline(block.replace(/^###\s+/, ""))}</h3>`);
    } else if (/^##\s+/.test(block)) {
      out.push(`<h2>${inline(block.replace(/^##\s+/, ""))}</h2>`);
    } else if (/^(-{3,}|\*{3,})$/.test(block)) {
      out.push("<hr>");
    } else if (block.split("\n").every((l) => /^&gt;\s?/.test(l))) {
      const inner = block.split("\n")
        .map((l) => l.replace(/^&gt;\s?/, "")).join(" ");
      out.push(`<blockquote><p>${inline(inner)}</p></blockquote>`);
    } else if (block.split("\n").every((l) => /^[-*]\s+/.test(l))) {
      const items = block.split("\n")
        .map((l) => `<li>${inline(l.replace(/^[-*]\s+/, ""))}</li>`).join("");
      out.push(`<ul>${items}</ul>`);
    } else if (block.split("\n").every((l) => /^\d+\.\s+/.test(l))) {
      const items = block.split("\n")
        .map((l) => `<li>${inline(l.replace(/^\d+\.\s+/, ""))}</li>`).join("");
      out.push(`<ol>${items}</ol>`);
    } else {
      out.push(`<p>${inline(block.replace(/\n/g, " "))}</p>`);
    }
  }
  return out.join("\n");
}

// ------------------------------------------------------------ page chrome

export function headHtml(title, description, canonical, extra = "") {
  return `<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>${esc(title)}</title>
    <meta name="description" content="${esc(description)}">
    <link rel="canonical" href="${canonical}">
    <meta property="og:title" content="${esc(title)}">
    <meta property="og:description" content="${esc(description)}">
    <meta property="og:url" content="${canonical}">
    <meta property="og:site_name" content="Joice">
    <meta property="og:image" content="${SITE}/assets/joice-icon.png">
    <meta name="twitter:card" content="summary">
    <link rel="alternate" type="application/rss+xml" title="${esc(BLOG_TITLE)}" href="${SITE}/blog/feed.xml">
    <link rel="icon" type="image/png" href="/assets/favicon.png">
    <link rel="apple-touch-icon" href="/assets/apple-touch-icon.png">
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Lora:ital,wght@0,400;0,500;0,600;1,400;1,500&family=DM+Sans:ital,opsz,wght@0,9..40,300;0,9..40,400;0,9..40,500;0,9..40,600;0,9..40,700;1,9..40,400&display=swap" rel="stylesheet">
    <link rel="stylesheet" href="/style.css">
${extra}</head>`;
}

export const NAV = `    <nav>
        <div class="nav-inner">
            <a href="/" class="logo"><img src="/assets/joice-icon.png" alt="">joice</a>
            <div class="nav-links">
                <a href="/#features">features</a>
                <a href="/blog/">blog</a>
                <a href="${APP_STORE_URL}" class="btn-nav">download</a>
            </div>
        </div>
    </nav>`;

export const FOOTER = `    <footer>
        <div class="footer-inner">
            <div class="footer-top">
                <div class="footer-brand">
                    <a href="/" class="logo"><img src="/assets/joice-icon.png" alt="">joice</a>
                    <p>A voice journal under a soft sky. Talk it out — Joice listens.</p>
                </div>
                <div class="footer-links">
                    <div class="footer-col">
                        <h4>product</h4>
                        <a href="/#features">features</a>
                        <a href="/blog/">blog</a>
                        <a href="${APP_STORE_URL}">download</a>
                    </div>
                    <div class="footer-col">
                        <h4>legal</h4>
                        <a href="/privacy.html">privacy policy</a>
                        <a href="/terms.html">terms of service</a>
                    </div>
                    <div class="footer-col">
                        <h4>support</h4>
                        <a href="/support.html">support center</a>
                        <a href="mailto:support@joiceapp.com">contact us</a>
                    </div>
                </div>
            </div>
            <div class="footer-bottom">
                <p>&copy; 2026 Joice. All rights reserved.</p>
                <p>Made with care, one conversation at a time.</p>
            </div>
        </div>
    </footer>`;

export function htmlResponse(body, status = 200, maxAge = 300) {
  return new Response(body, {
    status,
    headers: {
      "content-type": "text/html; charset=utf-8",
      "cache-control": `public, max-age=60, s-maxage=${maxAge}`,
    },
  });
}

export function errorPage(status, message) {
  const body = `${headHtml(message + " — joice", message, SITE + "/blog/")}
<body class="blog-body">
${NAV}
    <main class="blog-post-page">
        <a class="blog-back" href="/blog/">&larr; the joice journal</a>
        <h1>${esc(message)}</h1>
        <p class="blog-post-desc">Maybe the essay moved, or maybe it was never here. The journal itself is one click back.</p>
    </main>
${FOOTER}
</body>
</html>
`;
  return htmlResponse(body, status, 60);
}
