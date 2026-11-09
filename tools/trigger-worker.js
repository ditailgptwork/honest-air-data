// Cloudflare Worker (cron trigger): asks GitHub to run the hourly workflow.
// GitHub's own schedule drops most runs of this repository, so a second clock asks
// through workflow_dispatch. The workflow's check job skips the build when the last
// one is less than 45 minutes old, so extra calls cost a few seconds each.
//
// Setup (free plan): create a Worker, paste this file, add the secret GH_TOKEN
// (fine-grained token, only this repository, permission "Actions: Read and write"),
// and add a cron trigger such as "*/20 * * * *". The token never goes in this repository.
const URL = "https://api.github.com/repos/ditailgptwork/honest-air-data/actions/workflows/hourly.yml/dispatches";

export default {
  async scheduled(event, env, ctx) {
    const r = await fetch(URL, {
      method: "POST",
      headers: {
        "Authorization": `Bearer ${env.GH_TOKEN}`,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "not-fine-trigger",
      },
      body: JSON.stringify({ ref: "main" }),
    });
    if (r.status !== 204) console.log("dispatch failed", r.status, await r.text());
  },
};
