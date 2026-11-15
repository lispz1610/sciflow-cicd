// sciflow-cicd runtime-template=open-code-review version=1
"use strict";

const MAX_COUNT = 1_000_000;
const MAX_RETRIES = 3;
const MAX_FINDING_LINKS = 20;
const TRIAGE_LABEL = "needs-triage";
const BOT_LOGIN = "github-actions[bot]";
const BLOCK_START = "<!-- sciflow-cicd-ocr-advisory:managed-start -->";
const BLOCK_END = "<!-- sciflow-cicd-ocr-advisory:managed-end -->";
const STATUS_START = "<!-- sciflow-cicd-ocr-advisory:status-start -->";
const STATUS_END = "<!-- sciflow-cicd-ocr-advisory:status-end -->";
const FINDINGS_START = "<!-- sciflow-cicd-ocr-advisory:findings-start -->";
const FINDINGS_END = "<!-- sciflow-cicd-ocr-advisory:findings-end -->";
const HISTORY_START = "<!-- sciflow-cicd-ocr-advisory:history-start -->";
const HISTORY_END = "<!-- sciflow-cicd-ocr-advisory:history-end -->";
const HISTORY_ITEM_START = "<!-- sciflow-cicd-ocr-advisory:history-item-start -->";
const HISTORY_ITEM_END = "<!-- sciflow-cicd-ocr-advisory:history-item-end -->";

function positiveInteger(value) {
  const number = Number(value);
  return Number.isSafeInteger(number) && number > 0 ? number : null;
}

function escapeRegex(value) {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function count(value, name) {
  const text = typeof value === "number" && Number.isSafeInteger(value) ? String(value) : value;
  if (typeof text !== "string" || !/^(0|[1-9][0-9]*)$/.test(text)) {
    throw new Error(`Open Code Review output '${name}' is missing or is not a nonnegative integer.`);
  }
  const number = Number(text);
  if (!Number.isSafeInteger(number) || number > MAX_COUNT) {
    throw new Error(`Open Code Review output '${name}' exceeds the supported counter bound.`);
  }
  return number;
}

function isBot(item) {
  return item && item.user && (item.user.login === BOT_LOGIN || item.user.type === "Bot");
}

function markerFor(owner, repo, number) {
  return `<!-- sciflow-cicd-ocr-advisory:v1:${owner}/${repo}#${number} -->`;
}

function managedBlock(body) {
  const escapedStart = escapeRegex(BLOCK_START);
  const escapedEnd = escapeRegex(BLOCK_END);
  const pattern = `${escapedStart}[\\s\\S]*?${escapedEnd}`;
  const match = new RegExp(pattern).exec(body || "");
  return match && match[0];
}

function replaceManagedBlock(previous, generated) {
  const body = typeof previous === "string" ? previous : "";
  const old = managedBlock(body);
  if (old) return body.replace(old, generated);
  return body.trim() ? `${body.trimEnd()}\n\n${generated}` : generated;
}

function retryable(error) {
  const status = error && (error.status || (error.response && error.response.status));
  return status === 408 || status === 429 || status >= 500 ||
    ["ECONNRESET", "ETIMEDOUT", "EAI_AGAIN", "UND_ERR_CONNECT_TIMEOUT"].includes(error && error.code);
}

async function withRetry(operation, sleep) {
  for (let attempt = 0; ; attempt += 1) {
    try {
      return await operation();
    } catch (error) {
      if (!retryable(error) || attempt >= MAX_RETRIES - 1) throw error;
      await sleep(100 * (2 ** attempt));
    }
  }
}

function safeGithubUrl(candidate, serverUrl, expectedPath) {
  if (typeof candidate !== "string" || candidate.length > 500) return null;
  try {
    const url = new URL(candidate);
    const server = new URL(serverUrl);
    if (url.protocol !== "https:" || url.host !== server.host || url.pathname !== expectedPath ||
        url.username || url.password || url.search ||
        (url.hash && !/^#(?:issuecomment-[1-9][0-9]*|discussion_r[1-9][0-9]*)$/.test(url.hash))) return null;
    return url.href;
  } catch (_) {
    return null;
  }
}

function statusBlock({ headSha, runId, runAttempt, counters, serverUrl, owner, repo }) {
  const runUrl = `${serverUrl.replace(/\/$/, "")}/${owner}/${repo}/actions/runs/${runId}`;
  const lines = [
    STATUS_START,
    "### Latest review status",
    counters.total === 0
      ? "No new actionable findings were reported. Earlier findings below remain available for maintainer triage."
      : "Actionable findings were reported for this pull request.",
    `Latest reviewed head: \`${headSha}\``,
    `Latest workflow run: [#${runId} attempt ${runAttempt}](${runUrl})`,
    `Latest run counts: ${counters.total} total, ${counters.inline} inline, ${counters.routed} routed, ${counters.skipped} skipped, ${counters.failed} failed.`,
    STATUS_END,
  ];
  return lines.join("\n");
}

function findingsBlock({ prUrl, headSha, counters, summaryUrl, findingUrls }) {
  const lines = [
    FINDINGS_START,
    `## Findings from reviewed head \`${headSha}\``,
    "",
    `Pull request: <${prUrl}>`,
    `Findings reported: ${counters.total}`,
    `Inline comments posted: ${counters.inline}`,
    `Comments routed to PR summary: ${counters.routed}`,
    `Comments skipped as duplicates: ${counters.skipped}`,
    `Comments that failed to post: ${counters.failed}`,
  ];
  const links = [];
  if (summaryUrl) links.push(`[PR summary comment](${summaryUrl})`);
  findingUrls.forEach((url, index) => links.push(`[Inline review comment ${index + 1}](${url})`));
  if (links.length) lines.push("", "### Finding and comment links", "", ...links.map((link) => `- ${link}`));
  lines.push("", "### Human triage", "", "- [ ] Check the linked findings against the current pull request diff.",
    "- [ ] Confirm, dismiss, or request more information for each finding.",
    "- [ ] Assign an owner and track any accepted fix.", FINDINGS_END);
  return lines.join("\n");
}

function currentReport(body) {
  const sha = /<!-- sciflow-cicd-ocr-advisory-latest-head:([0-9a-f]{40}) -->/i.exec(body || "");
  const run = /<!-- sciflow-cicd-ocr-advisory-latest-run:([1-9][0-9]*):([1-9][0-9]*) -->/.exec(body || "");
  return {
    headSha: sha && sha[1].toLowerCase(),
    runId: run ? positiveInteger(run[1]) : null,
    runAttempt: run ? positiveInteger(run[2]) : null,
  };
}

function updateCleanBody(previous, { headSha, runId, runAttempt, counters, serverUrl, owner, repo }) {
  const body = typeof previous === "string" ? previous : "";
  const block = managedBlock(body);
  if (!block) throw new Error("The existing managed report has no replaceable report block.");
  const statusPattern = `${escapeRegex(STATUS_START)}\\s[\\s\\S]*?${escapeRegex(STATUS_END)}`;
  const updated = block
    .replace(/<!-- sciflow-cicd-ocr-advisory-latest-head:[0-9a-f]{40} -->/i,
      `<!-- sciflow-cicd-ocr-advisory-latest-head:${headSha} -->`)
    .replace(/<!-- sciflow-cicd-ocr-advisory-latest-run:[1-9][0-9]*:[1-9][0-9]* -->/,
      `<!-- sciflow-cicd-ocr-advisory-latest-run:${runId}:${runAttempt} -->`)
    .replace(new RegExp(statusPattern),
      statusBlock({ headSha, runId, runAttempt, counters, serverUrl, owner, repo }));
  return body.replace(block, updated);
}

function previousFindingHistory(previous, current) {
  const block = managedBlock(previous);
  if (!block) return "";
  const findingsPattern = `${escapeRegex(FINDINGS_START)}\\s[\\s\\S]*?${escapeRegex(FINDINGS_END)}`;
  const findings = new RegExp(findingsPattern).exec(block);
  const findingHead = /<!-- sciflow-cicd-ocr-advisory-finding-head:([0-9a-f]{40}) -->/i.exec(block);
  const findingRun = /<!-- sciflow-cicd-ocr-advisory-finding-run:([1-9][0-9]*):([1-9][0-9]*) -->/.exec(block);
  const historyPattern = `${escapeRegex(HISTORY_START)}\\s*([\\s\\S]*?)\\s*${escapeRegex(HISTORY_END)}`;
  const existingHistory = new RegExp(historyPattern).exec(block);
  const historyItems = [];
  if (existingHistory) {
    const itemPattern = new RegExp(`${escapeRegex(HISTORY_ITEM_START)}\\s*([\\s\\S]*?)\\s*${escapeRegex(HISTORY_ITEM_END)}`, "g");
    for (const match of existingHistory[1].matchAll(itemPattern)) historyItems.push(match[1].slice(0, 4_000));
  }
  const sameFindingRun = findingHead && findingRun && findingHead[1].toLowerCase() === current.headSha.toLowerCase() &&
    Number(findingRun[1]) === current.runId && Number(findingRun[2]) === current.runAttempt;
  if (findings && findingHead && !sameFindingRun) {
    historyItems.unshift(`<!-- sciflow-cicd-ocr-advisory:history-head:${findingHead[1]} -->\n${findings[0]}`);
  }
  const retained = historyItems.slice(0, 3);
  if (!retained.length) return "";
  return [HISTORY_START, "### Previous actionable reports", "", ...retained.flatMap((item) =>
    [HISTORY_ITEM_START, item, HISTORY_ITEM_END, ""]), HISTORY_END].join("\n");
}

function compareRuns(left, right) {
  if (!left.runId || !right.runId) return 0;
  if (left.runId !== right.runId) return left.runId < right.runId ? -1 : 1;
  if (!left.runAttempt || !right.runAttempt) return 0;
  return left.runAttempt === right.runAttempt ? 0 : (left.runAttempt < right.runAttempt ? -1 : 1);
}

function issueMatches(item, marker) {
  if (!item || item.pull_request || !isBot(item) || typeof item.body !== "string") {
    return false;
  }
  return item.body.includes(marker);
}

function commentMatches(item, marker) {
  return isBot(item) && typeof item.body === "string" && item.body.includes(marker);
}

async function listManagedIssue(github, owner, repo, marker) {
  const items = await github.paginate(github.rest.issues.listForRepo, {
    owner, repo, state: "all", per_page: 100,
  });
  return items.filter((item) => issueMatches(item, marker))
    .sort((left, right) => left.number - right.number)[0] || null;
}

async function ensureTriageLabel(github, owner, repo) {
  let labels = await github.paginate(github.rest.issues.listLabelsForRepo, {
    owner, repo, per_page: 100,
  });
  if (labels.some((label) => label.name === TRIAGE_LABEL)) return;
  try {
    await github.rest.issues.createLabel({
      owner, repo, name: TRIAGE_LABEL, color: "ededed", description: "Needs maintainer evaluation",
    });
  } catch (error) {
    if (!(error && error.status === 422)) throw error;
    labels = await github.paginate(github.rest.issues.listLabelsForRepo, {
      owner, repo, per_page: 100,
    });
    if (!labels.some((label) => label.name === TRIAGE_LABEL)) throw error;
  }
}

async function syncTriageLabel(github, owner, repo, issue, actionable, preserveClosed) {
  if (preserveClosed) return;
  const hasLabel = Array.isArray(issue.labels) && issue.labels.some((label) =>
    (typeof label === "string" ? label : label.name) === TRIAGE_LABEL);
  if (actionable && !hasLabel) {
    await ensureTriageLabel(github, owner, repo);
    await github.rest.issues.addLabels({ owner, repo, issue_number: issue.number, labels: [TRIAGE_LABEL] });
  }
}

async function findingLinks(github, owner, repo, number, headSha, serverUrl) {
  const comments = await github.paginate(github.rest.pulls.listReviewComments, {
    owner, repo, pull_number: number, per_page: 100,
  });
  return comments
    .filter((comment) => comment && comment.commit_id === headSha && isBot(comment))
    .map((comment) => safeGithubUrl(comment.html_url, serverUrl, `/${owner}/${repo}/pull/${number}`))
    .filter(Boolean)
    .slice(0, MAX_FINDING_LINKS);
}

async function upsertPullRequestLink(github, owner, repo, number, issueNumber, issueUrl, marker) {
  const linkMarker = marker.replace("sciflow-cicd-ocr-advisory:v1:", "sciflow-cicd-ocr-advisory-link:v1:");
  const body = `${linkMarker}\nAutomated Advisory Review findings are tracked in [issue #${issueNumber}](${issueUrl}).`;
  const comments = await github.paginate(github.rest.issues.listComments, {
    owner, repo, issue_number: number, per_page: 100,
  });
  const previous = comments.find((comment) => commentMatches(comment, linkMarker));
  if (previous) {
    if (previous.body !== body) {
      await github.rest.issues.updateComment({ owner, repo, comment_id: previous.id, body });
    }
    return "updated";
  }
  await github.rest.issues.createComment({ owner, repo, issue_number: number, body });
  return "created";
}

async function syncOnce({ github, context, counters }) {
  const owner = context.repo && context.repo.owner;
  const repo = context.repo && context.repo.repo;
  const payload = context.payload || {};
  const eventPr = payload.pull_request;
  const number = positiveInteger(eventPr && eventPr.number);
  const eventHead = eventPr && eventPr.head && eventPr.head.sha;
  const fullName = `${owner}/${repo}`;
  if (!owner || !repo || !number || !eventPr || !eventPr.head || !eventPr.base ||
      eventPr.head.repo?.full_name !== fullName || eventPr.base.repo?.full_name !== fullName) {
    return { status: "skipped", reason: "not a same-repository pull request" };
  }
  if (!/^[0-9a-f]{40}$/i.test(eventHead || "")) {
    return { status: "skipped", reason: "the event did not contain a valid PR head SHA" };
  }

  const parsed = {
    total: count(counters && counters.total, "comments_total"),
    inline: count(counters && counters.inline, "comments_inline"),
    routed: count(counters && counters.routed, "comments_routed"),
    skipped: count(counters && counters.skipped, "comments_skipped"),
    failed: count(counters && counters.failed, "comments_failed"),
  };
  const runId = positiveInteger(context.runId);
  const runAttempt = positiveInteger(context.runAttempt || 1);
  const serverUrl = typeof context.serverUrl === "string" ? context.serverUrl.replace(/\/$/, "") : "";
  if (!runId || !runAttempt || !serverUrl) return { status: "skipped", reason: "workflow identity is incomplete" };

  const response = await github.rest.pulls.get({ owner, repo, pull_number: number });
  const pull = response && response.data;
  if (!pull || pull.number !== number || !pull.head || pull.head.sha !== eventHead ||
      pull.head.repo?.full_name !== fullName || pull.base?.repo?.full_name !== fullName) {
    return { status: "stale", reason: "the PR identity or head no longer matches this trusted same-repository event" };
  }
  const prUrl = safeGithubUrl(pull.html_url, serverUrl, `/${owner}/${repo}/pull/${number}`);
  if (!prUrl) return { status: "skipped", reason: "GitHub returned an unexpected PR URL" };

  const marker = markerFor(owner, repo, number);
  const existing = await listManagedIssue(github, owner, repo, marker);
  const previous = currentReport(existing && existing.body);
  if (existing && compareRuns({ runId, runAttempt }, previous) < 0) {
    const issueUrl = safeGithubUrl(existing.html_url, serverUrl, `/${owner}/${repo}/issues/${existing.number}`);
    if (issueUrl) await upsertPullRequestLink(github, owner, repo, number, existing.number, issueUrl, marker);
    return { status: "stale-run", issueNumber: existing.number };
  }
  if (parsed.total === 0 && !existing) return { status: "clean" };

  const issueUrlForLink = existing && safeGithubUrl(existing.html_url, serverUrl,
    `/${owner}/${repo}/issues/${existing.number}`);
  if (parsed.total === 0) {
    if (!issueUrlForLink) return { status: "skipped", reason: "GitHub returned an unexpected issue URL" };
    const body = updateCleanBody(existing.body, {
      headSha: eventHead.toLowerCase(), runId, runAttempt, counters: parsed, serverUrl, owner, repo,
    });
    let issue = existing;
    let status = "clean-updated";
    if (existing.body !== body) {
      const updated = await github.rest.issues.update({ owner, repo, issue_number: existing.number, body });
      issue = updated.data;
    } else {
      status = "unchanged";
    }
    await upsertPullRequestLink(github, owner, repo, number, issue.number, issueUrlForLink, marker);
    return { status, issueNumber: issue.number };
  }

  const summaryPath = `/${owner}/${repo}/pull/${number}`;
  const summaryUrl = safeGithubUrl(counters && counters.summaryUrl, serverUrl, summaryPath);
  const inlineLinks = await findingLinks(github, owner, repo, number, eventHead, serverUrl);
  const history = existing ? previousFindingHistory(existing.body, { headSha: eventHead, runId, runAttempt }) : "";
  const generated = [
    BLOCK_START,
    marker,
    `<!-- sciflow-cicd-ocr-advisory-latest-head:${eventHead.toLowerCase()} -->`,
    `<!-- sciflow-cicd-ocr-advisory-latest-run:${runId}:${runAttempt} -->`,
    statusBlock({ headSha: eventHead.toLowerCase(), runId, runAttempt,
      counters: parsed, serverUrl, owner, repo }),
    `<!-- sciflow-cicd-ocr-advisory-finding-head:${eventHead.toLowerCase()} -->`,
    `<!-- sciflow-cicd-ocr-advisory-finding-run:${runId}:${runAttempt} -->`,
    findingsBlock({ prUrl, headSha: eventHead.toLowerCase(),
      counters: parsed, summaryUrl, findingUrls: inlineLinks }),
    history,
    BLOCK_END,
  ].filter(Boolean).join("\n");
  const body = replaceManagedBlock(existing && existing.body, generated);
  let issue;
  let status;
  const sameHeadClosed = Boolean(existing && existing.state === "closed" && previous.headSha === eventHead.toLowerCase());
  if (!existing) {
    await ensureTriageLabel(github, owner, repo);
    const created = await github.rest.issues.create({ owner, repo,
      title: `Advisory review findings: ${owner}/${repo}#${number}`, body, labels: [TRIAGE_LABEL] });
    issue = created.data;
    status = "created";
  } else {
    const issueUrl = safeGithubUrl(existing.html_url, serverUrl, `/${owner}/${repo}/issues/${existing.number}`);
    if (!issueUrl) return { status: "skipped", reason: "GitHub returned an unexpected issue URL" };
    const wasClosed = existing.state === "closed";
    const updates = {};
    if (existing.body !== body) updates.body = body;
    if (existing.title !== `Advisory review findings: ${owner}/${repo}#${number}`) {
      updates.title = `Advisory review findings: ${owner}/${repo}#${number}`;
    }
    if (wasClosed && !sameHeadClosed && parsed.total > 0) updates.state = "open";
    if (Object.keys(updates).length) {
      const updated = await github.rest.issues.update({ owner, repo, issue_number: existing.number, ...updates });
      issue = updated.data;
      status = wasClosed && !sameHeadClosed && parsed.total > 0 ? "reopened" : (parsed.total === 0 ? "clean-updated" : "updated");
    } else {
      issue = existing;
      status = sameHeadClosed ? "human-closed" : "unchanged";
    }
  }

  const newActionableHead = parsed.total > 0 &&
    (!existing || previous.headSha !== eventHead.toLowerCase());
  await syncTriageLabel(github, owner, repo, issue, newActionableHead, sameHeadClosed);

  const issueUrl = safeGithubUrl(issue && issue.html_url, serverUrl, `/${owner}/${repo}/issues/${issue.number}`);
  if (!issueUrl) throw new Error("GitHub returned an unexpected managed issue URL");
  await upsertPullRequestLink(github, owner, repo, number, issue.number, issueUrl, marker);
  return { status, issueNumber: issue.number };
}

async function syncReviewIssue({ github, context, counters, sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms)) }) {
  return withRetry(() => syncOnce({ github, context, counters }), sleep);
}

async function run({ github, context, core, counters }) {
  try {
    const result = await syncReviewIssue({ github, context, counters });
    if (result.status === "skipped" || result.status === "stale" || result.status === "stale-run") {
      core.info(`Advisory Review issue report ${result.status}: ${result.reason || "a newer report already exists"}.`);
    } else {
      core.info(`Advisory Review issue report ${result.status}${result.issueNumber ? ` (issue #${result.issueNumber})` : ""}.`);
    }
    return result;
  } catch (error) {
    core.setFailed(`Advisory Review findings issue report could not be synchronized: ${error.message}`);
    throw error;
  }
}

module.exports = { run, syncReviewIssue };
