// POST /api/generate — kick off a one-off blog post generation.
//
// Called by the review console's "generate now" button. Verifies the caller
// is an allowed reviewer (their Supabase session must pass review_ping),
// then dispatches the blog-generate GitHub Actions workflow with fast=true
// (streaming API instead of batches — someone is waiting).
//
// Requires a Cloudflare Pages environment secret GITHUB_DISPATCH_TOKEN:
// a fine-grained GitHub PAT for this repo with Actions read+write.

import { SUPABASE_URL, SUPABASE_KEY } from "../../functions-lib/blog.js";

const REPO = "teapotlabs/joice_website";
const WORKFLOW = "blog-generate.yml";

function json(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

export async function onRequestPost(context) {
  const auth = context.request.headers.get("authorization") || "";
  if (!auth.startsWith("Bearer ")) {
    return json({ error: "sign in first" }, 401);
  }

  // Reviewer gate: run the caller's own Supabase session through the
  // allowlist-checking RPC. Anyone not on allowed_reviewers gets a 42501.
  const ping = await fetch(`${SUPABASE_URL}/rest/v1/rpc/review_ping`, {
    method: "POST",
    headers: {
      apikey: SUPABASE_KEY,
      authorization: auth,
      "content-type": "application/json",
    },
    body: "{}",
  });
  if (!ping.ok) {
    return json({ error: "not on the reviewer list" }, 403);
  }

  const token = context.env.GITHUB_DISPATCH_TOKEN;
  if (!token) {
    return json({ error: "GITHUB_DISPATCH_TOKEN is not configured in Cloudflare Pages" }, 500);
  }

  const dispatch = await fetch(
    `https://api.github.com/repos/${REPO}/actions/workflows/${WORKFLOW}/dispatches`,
    {
      method: "POST",
      headers: {
        authorization: `Bearer ${token}`,
        accept: "application/vnd.github+json",
        "user-agent": "joice-review-console",
        "content-type": "application/json",
      },
      body: JSON.stringify({ ref: "main", inputs: { fast: "true" } }),
    },
  );
  if (dispatch.status !== 204) {
    const detail = (await dispatch.text()).slice(0, 200);
    return json({ error: `workflow dispatch failed (${dispatch.status}): ${detail}` }, 502);
  }
  return json({ ok: true });
}
