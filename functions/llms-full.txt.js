// /llms-full.txt — the llms.txt convention's "full" variant: complete post
// text in one plain-text document, so LLMs can ingest the blog without
// crawling every page.

import {
  SITE, APP_STORE_URL, BLOG_TITLE, BLOG_DESC, BRAND_BLURB,
  fetchPosts, isoDate,
} from "../functions-lib/blog.js";

export async function onRequestGet() {
  let posts = [];
  try {
    posts = await fetchPosts();
  } catch (err) {
    // serve the static portion regardless
  }

  const articles = posts.map((p) => {
    const sources = (p.sources || [])
      .map((s) => `- ${s.title} — ${s.publisher} — ${s.url}`).join("\n");
    return `---

# ${p.title}

URL: ${SITE}/blog/${p.slug}/
Published: ${isoDate(p.published_at)}
Tags: ${(p.tags || []).join(", ")}

${p.body_md.trim()}

Sources:
${sources || "(none)"}`;
  }).join("\n\n");

  const body = `# Joice — ${BLOG_TITLE} (full text)

> ${BRAND_BLURB}

${BLOG_DESC}

Joice is available for iOS: ${APP_STORE_URL}
Index of posts: ${SITE}/llms.txt | ${SITE}/blog/posts.json | RSS: ${SITE}/blog/feed.xml

${articles || "(first posts coming soon)"}
`;
  return new Response(body, {
    headers: {
      "content-type": "text/plain; charset=utf-8",
      "cache-control": "public, max-age=300, s-maxage=600",
    },
  });
}
