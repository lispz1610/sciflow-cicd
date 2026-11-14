const test = require('node:test');
const assert = require('node:assert/strict');

const { run, syncReviewIssue } = require('../scripts/review_issue_reporter.js');

function fixture({ headSha = 'a'.repeat(40), counters = {}, existingIssues = [], comments = [], reviewComments = [], repoLabels = ['needs-triage'] } = {}) {
  const state = { issues: existingIssues, comments, reviewComments, repoLabels, calls: [], currentHead: headSha };
  const github = {
    paginate: async (method, options) => (await method(options)).data,
    rest: {
      pulls: {
        get: async ({ pull_number }) => ({ data: {
          number: pull_number,
          head: { sha: state.currentHead, repo: { full_name: 'acme/widget' } },
          base: { repo: { full_name: 'acme/widget' } },
          html_url: 'https://github.com/acme/widget/pull/17',
        } }),
        listReviewComments: async () => ({ data: state.reviewComments }),
      },
      issues: {
        listForRepo: async () => ({ data: state.issues }),
        listLabelsForRepo: async () => ({ data: state.repoLabels }),
        createLabel: async ({ name }) => {
          state.repoLabels.push({ name });
          state.calls.push(['createLabel', name]);
          return { data: { name } };
        },
        create: async ({ title, body, labels }) => {
          const issue = { number: 91, title, body, labels: labels.map((name) => ({ name })), state: 'open', user: { login: 'github-actions[bot]', type: 'Bot' }, html_url: 'https://github.com/acme/widget/issues/91' };
          state.issues.push(issue);
          state.calls.push(['create', issue]);
          return { data: issue };
        },
        update: async ({ issue_number, ...updates }) => {
          const issue = state.issues.find((item) => item.number === issue_number);
          Object.assign(issue, updates);
          state.calls.push(['update', issue_number, updates]);
          return { data: issue };
        },
        addLabels: async ({ issue_number, labels }) => {
          const issue = state.issues.find((item) => item.number === issue_number);
          issue.labels = [...(issue.labels || []), ...labels.map((name) => ({ name }))];
          state.calls.push(['addLabels', issue_number, labels]);
          return { data: issue.labels };
        },
        removeLabel: async ({ issue_number, name }) => {
          const issue = state.issues.find((item) => item.number === issue_number);
          issue.labels = (issue.labels || []).filter((label) => label.name !== name);
          state.calls.push(['removeLabel', issue_number, name]);
          return { data: issue.labels };
        },
        listComments: async () => ({ data: state.comments }),
        createComment: async ({ body }) => {
          const comment = { id: 44, body, user: { login: 'github-actions[bot]', type: 'Bot' } };
          state.comments.push(comment);
          state.calls.push(['createComment', comment]);
          return { data: comment };
        },
        updateComment: async ({ comment_id, body }) => {
          const comment = state.comments.find((item) => item.id === comment_id);
          comment.body = body;
          state.calls.push(['updateComment', comment_id]);
          return { data: comment };
        },
      },
    },
  };
  const context = {
    repo: { owner: 'acme', repo: 'widget' },
    serverUrl: 'https://github.com',
    runId: 700,
    runAttempt: 1,
    payload: { pull_request: {
      number: 17,
      head: { sha: headSha, repo: { full_name: 'acme/widget' } },
      base: { repo: { full_name: 'acme/widget' } },
    } },
  };
  return { state, github, context, counters: { total: '2', inline: '1', routed: '1', skipped: '0', failed: '0', ...counters } };
}

test('actionable findings create one managed issue with routed and skipped counts plus a separate PR link', async () => {
  const value = fixture({ counters: {
    skipped: '2',
    summaryUrl: 'https://github.com/acme/widget/pull/17#issuecomment-55',
  }, reviewComments: [{
    commit_id: 'a'.repeat(40),
    html_url: 'https://github.com/acme/widget/pull/17#discussion_r62',
    user: { login: 'github-actions[bot]', type: 'Bot' },
  }] });
  const result = await syncReviewIssue({ ...value, sleep: async () => {} });
  assert.equal(result.status, 'created');
  assert.equal(value.state.issues.length, 1);
  assert.match(value.state.issues[0].body, /Comments routed to PR summary: 1/);
  assert.match(value.state.issues[0].body, /Inline comments posted: 1/);
  assert.match(value.state.issues[0].body, /Comments skipped as duplicates: 2/);
  assert.match(value.state.issues[0].body, /issuecomment-55/);
  assert.match(value.state.issues[0].body, /discussion_r62/);
  assert.deepEqual(value.state.issues[0].labels.map((label) => label.name), ['needs-triage']);
  assert.equal(value.state.comments.length, 1);
  assert.match(value.state.comments[0].body, /issues\/91/);
  assert.match(value.state.comments[0].body, /\[issue #91\]\(https:\/\/github\.com\/acme\/widget\/issues\/91\)/);
});

test('a missing needs-triage label is created before the report issue', async () => {
  const value = fixture({ repoLabels: [] });
  await syncReviewIssue({ ...value, sleep: async () => {} });
  assert.deepEqual(value.state.repoLabels.map((label) => label.name), ['needs-triage']);
  assert.deepEqual(value.state.issues[0].labels.map((label) => label.name), ['needs-triage']);
  assert.equal(value.state.calls[0][0], 'createLabel');
});

test('clean review output does not create a report', async () => {
  const value = fixture({ counters: { total: '0' } });
  const result = await syncReviewIssue({ ...value, sleep: async () => {} });
  assert.equal(result.status, 'clean');
  assert.equal(value.state.calls.length, 0);
});

test('a clean later run updates the existing report without closing it', async () => {
  const value = fixture();
  await syncReviewIssue({ ...value, sleep: async () => {} });
  value.context.runId = 701;
  value.counters.total = '0';
  value.counters.inline = '0';
  value.counters.routed = '0';
  const result = await syncReviewIssue({ ...value, sleep: async () => {} });
  assert.equal(result.status, 'clean-updated');
  assert.equal(value.state.issues.length, 1);
  assert.equal(value.state.issues[0].state, 'open');
  assert.match(value.state.issues[0].body, /No new actionable findings were reported/);
  assert.match(value.state.issues[0].body, /Findings reported: 2/);
  assert.match(value.state.issues[0].body, /Inline comments posted: 1/);
  assert.deepEqual(value.state.issues[0].labels.map((label) => label.name), ['needs-triage']);
});

test('retries find an issue created before an ambiguous API failure', async () => {
  const value = fixture();
  const create = value.github.rest.issues.create;
  let failAfterCreate = true;
  value.github.rest.issues.create = async (args) => {
    const created = await create(args);
    if (failAfterCreate) {
      failAfterCreate = false;
      const error = new Error('temporary service error');
      error.status = 503;
      throw error;
    }
    return created;
  };
  const result = await syncReviewIssue({ ...value, sleep: async () => {} });
  assert.equal(result.issueNumber, 91);
  assert.equal(value.state.issues.length, 1);
  assert.equal(value.state.comments.length, 1);
});

test('duplicate delivery updates the same issue and PR link', async () => {
  const value = fixture();
  await syncReviewIssue({ ...value, sleep: async () => {} });
  const firstBody = value.state.issues[0].body;
  const result = await syncReviewIssue({ ...value, sleep: async () => {} });
  assert.equal(result.status, 'unchanged');
  assert.equal(value.state.issues.length, 1);
  assert.equal(value.state.comments.length, 1);
  assert.equal(value.state.issues[0].body, firstBody);
});

test('same-head duplicate delivery preserves a human removal of needs-triage', async () => {
  const value = fixture();
  await syncReviewIssue({ ...value, sleep: async () => {} });
  value.state.issues[0].labels = [];
  value.state.calls.length = 0;
  const result = await syncReviewIssue({ ...value, sleep: async () => {} });
  assert.equal(result.status, 'unchanged');
  assert.deepEqual(value.state.issues[0].labels, []);
  assert.equal(value.state.calls.some(([name]) => name === 'addLabels'), false);
});

test('a human-closed report stays closed for the same PR head', async () => {
  const value = fixture();
  await syncReviewIssue({ ...value, sleep: async () => {} });
  value.state.issues[0].state = 'closed';
  value.state.calls.length = 0;
  const result = await syncReviewIssue({ ...value, sleep: async () => {} });
  assert.equal(result.status, 'human-closed');
  assert.equal(value.state.issues[0].state, 'closed');
  assert.equal(value.state.calls.length, 0);
});

test('a same-head clean run updates a human-closed report without reopening or relabeling it', async () => {
  const value = fixture();
  await syncReviewIssue({ ...value, sleep: async () => {} });
  value.state.issues[0].state = 'closed';
  value.context.runId = 701;
  value.counters.total = '0';
  value.counters.inline = '0';
  value.counters.routed = '0';
  const result = await syncReviewIssue({ ...value, sleep: async () => {} });
  assert.equal(result.status, 'clean-updated');
  assert.equal(value.state.issues[0].state, 'closed');
  assert.deepEqual(value.state.issues[0].labels.map((label) => label.name), ['needs-triage']);
  assert.match(value.state.issues[0].body, /No new actionable findings were reported/);
  assert.match(value.state.issues[0].body, /Findings reported: 2/);
});

test('same-head later actionable findings update but do not reopen human-closed reports', async () => {
  const value = fixture();
  await syncReviewIssue({ ...value, sleep: async () => {} });
  value.state.issues[0].state = 'closed';
  value.context.runId = 701;
  const result = await syncReviewIssue({ ...value, sleep: async () => {} });
  assert.equal(result.status, 'updated');
  assert.equal(value.state.issues[0].state, 'closed');
  assert.deepEqual(value.state.issues[0].labels.map((label) => label.name), ['needs-triage']);
});

test('a report for a newer PR head reopens the managed issue', async () => {
  const value = fixture();
  await syncReviewIssue({ ...value, sleep: async () => {} });
  value.state.issues[0].state = 'closed';
  const newHead = 'b'.repeat(40);
  value.state.currentHead = newHead;
  value.state.issues[0].labels = [];
  value.context.runId = 701;
  value.counters.total = '1';
  value.counters.inline = '1';
  value.counters.routed = '0';
  value.context.payload.pull_request.head.sha = newHead;
  const result = await syncReviewIssue({ ...value, sleep: async () => {} });
  assert.equal(result.status, 'reopened');
  assert.equal(value.state.issues[0].state, 'open');
  assert.deepEqual(value.state.issues[0].labels.map((label) => label.name), ['needs-triage']);
  assert.ok(value.state.issues[0].body.includes(`Latest reviewed head: \`${newHead}\``));
  assert.match(value.state.issues[0].body, /Findings reported: 1/);
  assert.match(value.state.issues[0].body, /Previous actionable reports/);
  assert.match(value.state.issues[0].body, /Findings reported: 2/);
});

test('a queued run for an old PR head cannot publish after the head moves', async () => {
  const value = fixture();
  value.state.currentHead = 'b'.repeat(40);
  const result = await syncReviewIssue({ ...value, sleep: async () => {} });
  assert.equal(result.status, 'stale');
  assert.equal(value.state.calls.length, 0);
});

test('a PR that changes to a fork is never reported', async () => {
  const value = fixture();
  value.github.rest.pulls.get = async ({ pull_number }) => ({ data: {
    number: pull_number,
    head: { sha: value.state.currentHead, repo: { full_name: 'contributor/widget' } },
    base: { repo: { full_name: 'acme/widget' } },
    html_url: 'https://github.com/acme/widget/pull/17',
  } });
  const result = await syncReviewIssue({ ...value, sleep: async () => {} });
  assert.equal(result.status, 'stale');
  assert.equal(value.state.calls.length, 0);
});

test('hostile action output is bounded to validated counters and GitHub URLs', async () => {
  const value = fixture({ counters: {
    summaryUrl: 'https://evil.example/steal?x=1',
  } });
  await syncReviewIssue({ ...value, sleep: async () => {} });
  const body = value.state.issues[0].body;
  assert.match(body, /Comments routed to PR summary: 1/);
  assert.doesNotMatch(body, /evil\.example|steal/);
  assert.ok(body.length < 4_000);
});

test('blank or malformed action counters fail closed instead of appearing clean', async () => {
  const value = fixture({ counters: { total: '' } });
  await assert.rejects(syncReviewIssue({ ...value, sleep: async () => {} }), /comments_total.*missing/);
  assert.equal(value.state.calls.length, 0);
});

test('persistent API failures are retried and reported as a failed reporter run', async () => {
  const value = fixture();
  let attempts = 0;
  value.github.rest.pulls.get = async () => {
    attempts += 1;
    const error = new Error('temporary API outage');
    error.status = 503;
    throw error;
  };
  let diagnostic = '';
  await assert.rejects(run({
    ...value,
    core: { info() {}, setFailed(message) { diagnostic = message; } },
  }), /temporary API outage/);
  assert.equal(attempts, 3);
  assert.match(diagnostic, /could not be synchronized/);
});
