// /blog/* — server-rendered from Supabase on every request.
// Routes: /blog/  /blog/<slug>/  /blog/feed.xml  /blog/posts.json

import {
  SITE, APP_STORE_URL, BLOG_TITLE, BLOG_DESC,
  fetchPosts, validSlug, esc, isoDate, prettyDate, monthLabel,
  readingMinutes, mdToHtml, headHtml, NAV, FOOTER, htmlResponse, errorPage,
} from "../../functions-lib/blog.js";

export async function onRequestGet(context) {
  const segments = (context.params.path || []).filter(Boolean);
  try {
    // canonicalize: /blog and /blog/<slug> redirect to trailing-slash form
    const url = new URL(context.request.url);
    const isFile = segments.length === 1 && segments[0].includes(".");
    if (!isFile && !url.pathname.endsWith("/")) {
      return Response.redirect(`${SITE}${url.pathname}/`, 301);
    }
    if (segments.length === 0) {
      return renderIndex();
    }
    if (segments.length === 1 && segments[0] === "feed.xml") {
      return renderFeed();
    }
    if (segments.length === 1 && segments[0] === "posts.json") {
      return renderPostsJson();
    }
    if (segments.length === 1) {
      return renderPost(segments[0]);
    }
    return errorPage(404, "page not found");
  } catch (err) {
    return errorPage(503, "the journal is catching its breath");
  }
}

// ------------------------------------------------------------------- post

function relatedPosts(post, all, count = 2) {
  const mine = new Set(post.tags || []);
  return all
    .filter((p) => p.slug !== post.slug)
    .map((p) => [
      (p.tags || []).reduce((n, t) => n + (mine.has(t) ? 1 : 0), 0), p,
    ])
    .sort((a, b) => b[0] - a[0])
    .slice(0, count)
    .map(([, p]) => p);
}

async function renderPost(slug) {
  if (!validSlug(slug)) return errorPage(404, "essay not found");
  const all = await fetchPosts();
  const post = all.find((p) => p.slug === slug);
  if (!post) return errorPage(404, "essay not found");

  const url = `${SITE}/blog/${post.slug}/`;
  const minutes = readingMinutes(post.body_md);
  const sources = post.sources || [];
  const tags = (post.tags || []).map((t) =>
    `                    <a class="pill blog-tag" href="/blog/?tag=${encodeURIComponent(t)}">${esc(t)}</a>`
  ).join("\n");
  const sourceItems = sources.map((s) =>
    `                    <li><a href="${esc(s.url)}" rel="nofollow noopener">${esc(s.title)}</a><span class="blog-source-pub"> — ${esc(s.publisher || "")}</span></li>`
  ).join("\n");
  const sourcesSection = sources.length ? `
                <aside class="blog-sources">
                    <h2>sources</h2>
                    <ul>
${sourceItems}
                    </ul>
                </aside>` : "";

  const jsonLd = {
    "@context": "https://schema.org",
    "@type": "BlogPosting",
    headline: post.title,
    description: post.description,
    datePublished: isoDate(post.published_at),
    url,
    keywords: (post.tags || []).join(", "),
    author: { "@type": "Organization", name: "Joice", url: SITE },
    publisher: {
      "@type": "Organization", name: "Joice", url: SITE,
      logo: { "@type": "ImageObject", url: `${SITE}/assets/joice-icon.png` },
    },
    mainEntityOfPage: url,
    citation: sources.map((s) => ({
      "@type": "CreativeWork", name: s.title, url: s.url,
    })),
    isPartOf: { "@type": "Blog", name: BLOG_TITLE, url: `${SITE}/blog/` },
  };
  const breadcrumbs = {
    "@context": "https://schema.org",
    "@type": "BreadcrumbList",
    itemListElement: [
      { "@type": "ListItem", position: 1, name: "joice", item: SITE + "/" },
      { "@type": "ListItem", position: 2, name: BLOG_TITLE, item: `${SITE}/blog/` },
      { "@type": "ListItem", position: 3, name: post.title, item: url },
    ],
  };
  const articleMeta = [
    '    <meta property="og:type" content="article">',
    `    <meta property="article:published_time" content="${isoDate(post.published_at)}">`,
    ...(post.tags || []).map((t) =>
      `    <meta property="article:tag" content="${esc(t)}">`),
  ].join("\n") + "\n";
  const extra = articleMeta +
    `    <script type="application/ld+json">\n${JSON.stringify(jsonLd, null, 2)}\n    </script>\n` +
    `    <script type="application/ld+json">\n${JSON.stringify(breadcrumbs, null, 2)}\n    </script>\n`;

  const related = relatedPosts(post, all);
  const relatedSection = related.length ? `
            <aside class="blog-related">
                <h2>more from the journal</h2>
                <div class="blog-related-grid">
${related.map((p) => `                    <a class="blog-card" href="/blog/${esc(p.slug)}/">
                        <div class="blog-card-meta">
                            <span>${prettyDate(p.published_at)}</span>
                            <span class="blog-meta-dot">&middot;</span>
                            <span>${readingMinutes(p.body_md)} min read</span>
                        </div>
                        <h3>${esc(p.title)}</h3>
                        <p>${esc(p.description)}</p>
                    </a>`).join("\n")}
                </div>
            </aside>` : "";

  const body = `${headHtml(`${post.title} — ${BLOG_TITLE}`, post.description, url, extra)}
<body class="blog-body">
${NAV}

    <main class="blog-post-page">
        <article>
            <header class="blog-post-head">
                <a class="blog-back" href="/blog/">&larr; the joice journal</a>
                <div class="blog-post-meta">
                    <span>${prettyDate(post.published_at)}</span>
                    <span class="blog-meta-dot">&middot;</span>
                    <span>${minutes} min read</span>
                </div>
                <h1>${esc(post.title)}</h1>
                <p class="blog-post-desc">${esc(post.description)}</p>
                <div class="blog-post-tags">
${tags}
                </div>
            </header>

            <div class="blog-post-body">
${mdToHtml(post.body_md)}
            </div>
${sourcesSection}

            <aside class="blog-app-cta">
                <img src="/assets/leaf-branch.png" alt="" class="blog-cta-leaf">
                <div>
                    <h2>talk it out with joice</h2>
                    <p>A voice journal that listens like a friend. Free to try on iOS.</p>
                </div>
                <a href="${APP_STORE_URL}" class="btn-primary">get the app</a>
            </aside>
        </article>
${relatedSection}
    </main>

${FOOTER}
</body>
</html>
`;
  return htmlResponse(body);
}

// ------------------------------------------------------------------ index

async function renderIndex() {
  const posts = await fetchPosts();
  const allTags = [...new Set(posts.flatMap((p) => p.tags || []))].sort();
  const chips = allTags.map((t) =>
    `                <button class="blog-chip" data-tag="${esc(t)}">${esc(t)}</button>`
  ).join("\n");

  const cards = [];
  let lastMonth = null;
  for (const p of posts) {
    const month = monthLabel(p.published_at);
    if (month !== lastMonth) {
      cards.push(`                <div class="blog-month" data-month>${month}</div>`);
      lastMonth = month;
    }
    const tagPills = (p.tags || []).map((t) =>
      `<span class="pill blog-tag">${esc(t)}</span>`).join("");
    cards.push(`                <a class="blog-card" href="/blog/${esc(p.slug)}/"
                   data-title="${esc(p.title.toLowerCase())}" data-desc="${esc(p.description.toLowerCase())}" data-tags="${esc((p.tags || []).join("|").toLowerCase())}" data-date="${isoDate(p.published_at)}">
                    <div class="blog-card-meta">
                        <span>${prettyDate(p.published_at)}</span>
                        <span class="blog-meta-dot">&middot;</span>
                        <span>${readingMinutes(p.body_md)} min read</span>
                    </div>
                    <h2>${esc(p.title)}</h2>
                    <p>${esc(p.description)}</p>
                    <div class="blog-card-tags">${tagPills}</div>
                </a>`);
  }
  const empty = posts.length ? "" :
    '                <p class="blog-empty">first essays are on their way.</p>';

  const blogLd = {
    "@context": "https://schema.org",
    "@type": "Blog",
    name: BLOG_TITLE,
    description: BLOG_DESC,
    url: `${SITE}/blog/`,
    publisher: { "@type": "Organization", name: "Joice", url: SITE },
    blogPost: posts.map((p) => ({
      "@type": "BlogPosting",
      headline: p.title,
      url: `${SITE}/blog/${p.slug}/`,
      datePublished: isoDate(p.published_at),
    })),
  };
  const indexExtra =
    `    <script type="application/ld+json">\n${JSON.stringify(blogLd, null, 2)}\n    </script>\n`;

  const body = `${headHtml(`${BLOG_TITLE} — essays on journaling & mental health`, BLOG_DESC, `${SITE}/blog/`, indexExtra)}
<body class="blog-body">
${NAV}

    <header class="blog-hero">
        <div class="section-inner">
            <span class="eyebrow">essays from joice</span>
            <h1>${esc(BLOG_TITLE)}</h1>
            <p class="blog-hero-sub">${esc(BLOG_DESC)}</p>
            <div class="blog-search-wrap">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>
                <input id="blog-search" type="search" placeholder="search the journal&hellip;" autocomplete="off">
            </div>
            <div class="blog-chips">
${chips}
            </div>
        </div>
    </header>

    <main class="blog-timeline">
        <div class="section-inner">
            <div class="blog-timeline-rail">
${cards.join("\n")}
${empty}
                <p class="blog-empty" id="blog-no-results" style="display:none">nothing matches that search — try another word.</p>
            </div>
        </div>
    </main>

${FOOTER}
    <script>
        const input = document.getElementById('blog-search');
        const cards = Array.from(document.querySelectorAll('.blog-card'));
        const months = Array.from(document.querySelectorAll('[data-month]'));
        const chips = Array.from(document.querySelectorAll('.blog-chip'));
        const empty = document.getElementById('blog-no-results');
        let activeTag = null;

        function apply() {
            const q = input.value.trim().toLowerCase();
            let shown = 0;
            cards.forEach(card => {
                const hay = card.dataset.title + ' ' + card.dataset.desc + ' ' +
                            card.dataset.tags.split('|').join(' ') + ' ' + card.dataset.date;
                const matchesText = !q || hay.includes(q);
                const matchesTag = !activeTag || card.dataset.tags.split('|').includes(activeTag);
                const show = matchesText && matchesTag;
                card.style.display = show ? '' : 'none';
                if (show) shown++;
            });
            months.forEach(m => {
                let node = m.nextElementSibling, visible = false;
                while (node && !node.hasAttribute('data-month')) {
                    if (node.classList.contains('blog-card') && node.style.display !== 'none') visible = true;
                    node = node.nextElementSibling;
                }
                m.style.display = visible ? '' : 'none';
            });
            if (empty) empty.style.display = shown ? 'none' : '';
        }

        input.addEventListener('input', apply);
        chips.forEach(chip => chip.addEventListener('click', () => {
            activeTag = activeTag === chip.dataset.tag ? null : chip.dataset.tag;
            chips.forEach(c => c.classList.toggle('active', c.dataset.tag === activeTag));
            apply();
        }));

        const urlTag = new URLSearchParams(location.search).get('tag');
        if (urlTag) {
            const chip = chips.find(c => c.dataset.tag === urlTag);
            if (chip) chip.click();
        }
    </script>
</body>
</html>
`;
  return htmlResponse(body);
}

// ------------------------------------------------------------- feed + json

function rfc2822(iso) {
  return new Date(`${isoDate(iso)}T12:00:00Z`).toUTCString().replace("GMT", "+0000");
}

async function renderFeed() {
  const posts = await fetchPosts();
  const items = posts.map((p) => `    <item>
      <title>${esc(p.title)}</title>
      <link>${SITE}/blog/${p.slug}/</link>
      <guid isPermaLink="true">${SITE}/blog/${p.slug}/</guid>
      <pubDate>${rfc2822(p.published_at)}</pubDate>
      <description>${esc(p.description)}</description>
    </item>`).join("\n");
  const lastBuild = posts.length ? rfc2822(posts[0].published_at)
    : rfc2822("2026-01-01");
  const xml = `<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">
  <channel>
    <title>${esc(BLOG_TITLE)}</title>
    <link>${SITE}/blog/</link>
    <atom:link href="${SITE}/blog/feed.xml" rel="self" type="application/rss+xml"/>
    <description>${esc(BLOG_DESC)}</description>
    <language>en-us</language>
    <lastBuildDate>${lastBuild}</lastBuildDate>
${items}
  </channel>
</rss>
`;
  return new Response(xml, {
    headers: {
      "content-type": "application/rss+xml; charset=utf-8",
      "cache-control": "public, max-age=300, s-maxage=600",
    },
  });
}

async function renderPostsJson() {
  const posts = await fetchPosts();
  const data = posts.map((p) => ({
    title: p.title,
    slug: p.slug,
    url: `${SITE}/blog/${p.slug}/`,
    date: isoDate(p.published_at),
    description: p.description,
    tags: p.tags || [],
    reading_minutes: readingMinutes(p.body_md),
    word_count: (p.body_md.match(/\w+/g) || []).length,
    sources: p.sources || [],
  }));
  return new Response(JSON.stringify(data, null, 2) + "\n", {
    headers: {
      "content-type": "application/json; charset=utf-8",
      "cache-control": "public, max-age=300, s-maxage=600",
      "access-control-allow-origin": "*",
    },
  });
}
