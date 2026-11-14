"use strict";

const fs = require("node:fs");
const { syncReviewIssue } = require("../scripts/review_issue_reporter.js");

async function main() {
  const input = JSON.parse(fs.readFileSync(0, "utf8"));
  const state = input.state;
  const github = {
    paginate: async (method, options) => (await method(options)).data,
    rest: {
      pulls: {
        get: async ({ pull_number }) => ({ data: {
          number: pull_number,
          head: { sha: state.currentHead || input.sha, repo: { full_name: "acme/project" } },
          base: { repo: { full_name: "acme/project" } },
          html_url: `https://github.com/acme/project/pull/${pull_number}`,
        } }),
        listReviewComments: async () => ({ data: [] }),
      },
      issues: {
        listForRepo: async () => ({ data: state.issues }),
        listLabelsForRepo: async () => ({ data: state.labels.map((name) => ({ name })) }),
        createLabel: async ({ name }) => { state.labels.push(name); return { data: { name } }; },
        create: async ({ title, body, labels }) => {
          const number = Math.max(70, ...state.issues.map((issue) => issue.number)) + 1;
          const issue = { number, title, body, labels: labels.map((name) => ({ name })), state: "open",
            user: { login: "github-actions[bot]", type: "Bot" },
            html_url: `https://github.com/acme/project/issues/${number}` };
          state.issues.push(issue);
          return { data: issue };
        },
        update: async ({ issue_number, ...updates }) => {
          const issue = state.issues.find((entry) => entry.number === issue_number);
          Object.assign(issue, updates);
          return { data: issue };
        },
        addLabels: async ({ issue_number, labels }) => {
          const issue = state.issues.find((entry) => entry.number === issue_number);
          issue.labels.push(...labels.map((name) => ({ name })));
          return { data: issue.labels };
        },
        removeLabel: async ({ issue_number, name }) => {
          const issue = state.issues.find((entry) => entry.number === issue_number);
          issue.labels = issue.labels.filter((label) => label.name !== name);
          return { data: issue.labels };
        },
        listComments: async () => ({ data: state.comments }),
        createComment: async ({ body }) => {
          const comment = { id: state.comments.length + 1, body,
            user: { login: "github-actions[bot]", type: "Bot" } };
          state.comments.push(comment);
          return { data: comment };
        },
        updateComment: async ({ comment_id, body }) => {
          const comment = state.comments.find((entry) => entry.id === comment_id);
          comment.body = body;
          return { data: comment };
        },
      },
      repos: {
        createCommitStatus: async ({ sha, state: status, context, description, target_url }) => {
          const entry = { sha, state: status, context, description, target_url };
          state.statuses.push(entry);
          return { data: entry };
        },
      },
    },
  };
  const sha = input.sha || "a".repeat(40);
  const result = await syncReviewIssue({
    github,
    context: {
      repo: { owner: "acme", repo: "project" }, serverUrl: "https://github.com",
      runId: input.runId || 44, runAttempt: 1,
      payload: { pull_request: { number: 3, head: { sha, repo: { full_name: "acme/project" } },
        base: { repo: { full_name: "acme/project" } } } },
    },
    counters: input.findings
      ? { total: "2", inline: "1", routed: "1", skipped: "0", failed: "0" }
      : { total: "0", inline: "0", routed: "0", skipped: "0", failed: "0" },
    sleep: async () => {},
  });
  process.stdout.write(JSON.stringify({ ...state, result }));
}

main().catch((error) => { console.error(error); process.exitCode = 1; });
