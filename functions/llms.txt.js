// /llms.txt — LLM-facing site map (llmstxt.org convention), regenerated
// from Supabase on request.

import {
  SITE, APP_STORE_URL, BLOG_TITLE, BLOG_DESC, BRAND_BLURB, fetchPosts,
} from "../functions-lib/blog.js";

export async function onRequestGet() {
  let posts = [];
  try {
    posts = await fetchPosts();
  } catch (err) {
    // serve the static portion regardless
  }
  const postLines = posts.map((p) =>
    `- [${p.title}](${SITE}/blog/${p.slug}/): ${p.description}`).join("\n");

  const body = `# Joice

> ${BRAND_BLURB}

Joice is available for iOS. Download: ${APP_STORE_URL}

## Blog — ${BLOG_TITLE}

${BLOG_DESC}
Machine-readable index: ${SITE}/blog/posts.json
RSS: ${SITE}/blog/feed.xml

${postLines || "(first posts coming soon)"}

## Pages

- [Home](${SITE}/): what Joice does and how it works
- [Support](${SITE}/support.html): help center and contact
- [Privacy](${SITE}/privacy.html): privacy policy
- [Terms](${SITE}/terms.html): terms of service
`;
  return new Response(body, {
    headers: {
      "content-type": "text/plain; charset=utf-8",
      "cache-control": "public, max-age=300, s-maxage=600",
    },
  });
}
