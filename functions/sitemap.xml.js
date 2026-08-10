// /sitemap.xml — regenerated from Supabase on request so newly published
// posts appear without a deploy.

import { SITE, fetchPosts, lastModified } from "../functions-lib/blog.js";

export async function onRequestGet() {
  let posts = [];
  try {
    posts = await fetchPosts();
  } catch (err) {
    // fall through with static pages only rather than erroring the sitemap
  }
  const staticPages = ["", "privacy.html", "terms.html", "support.html", "blog/"];
  const urls = staticPages.map((page) => `  <url>
    <loc>${SITE}/${page}</loc>
  </url>`);
  for (const p of posts) {
    urls.push(`  <url>
    <loc>${SITE}/blog/${p.slug}/</loc>
    <lastmod>${lastModified(p)}</lastmod>
  </url>`);
  }
  const xml = `<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
${urls.join("\n")}
</urlset>
`;
  return new Response(xml, {
    headers: {
      "content-type": "application/xml; charset=utf-8",
      "cache-control": "public, max-age=300, s-maxage=600",
    },
  });
}
