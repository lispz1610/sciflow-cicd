"""Render the trusted Advisory Review GitHub Actions workflow.

Adapted from open-code-review-setup commit 6e33cffa673eb4d017f43dd1a3f16500097992a9.
"""

from __future__ import annotations

import json

try:
    from scripts.review_policy import ReviewPolicy
except ImportError:
    from review_policy import ReviewPolicy


WORKFLOW_PATH = ".github/workflows/open-code-review.yml"
NEW_MARKER = "# sciflow-cicd workflow-template=open-code-review version=1"
LEGACY_MARKER = "# github-repo-setup workflow-template="

# Observed on 2026-10-09 in action.yml at the historical candidate commit.
# The upload-artifact input is false in our workflow, but the composite action
# still references it and restricted Actions policies may require it allowed.
UPSTREAM_ACTION_SOURCE = (
    "https://github.com/alibaba/open-code-review/blob/"
    "182898cf522da3d04157b422752d028417974e19/action.yml"
)
UPSTREAM_ACTION_DEPENDENCIES = (
    "actions/setup-node@820762786026740c76f36085b0efc47a31fe5020",
    "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
    "actions/github-script@3a2844b7e9c422d3c10d287c895573f7108da1b3",
    "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02",
)


def _yaml_string(value: str) -> str:
    """Quote a string using JSON's syntax, which is a safe YAML scalar form."""
    return json.dumps(value, ensure_ascii=True)


def render_workflow(policy: ReviewPolicy) -> bytes:
    """Return deterministic UTF-8 bytes for the fixed advisory workflow path."""
    workflow = policy.workflow
    lines = [
        NEW_MARKER,
        'name: "Open Code Review"',
        'run-name: "Review PR #${{ github.event.pull_request.number }} workflow ${{ github.workflow_sha }}"',
        "on:",
        "  pull_request_target:",
        "    types: [" + ", ".join(workflow.events) + "]",
        "permissions:",
        "  contents: read",
        "  pull-requests: write",
        "concurrency:",
        '  group: "ocr-${{ github.event.pull_request.number }}"',
        "  cancel-in-progress: true",
        "jobs:",
        "  review:",
        "    if: github.event.pull_request.head.repo.full_name == github.repository",
        "    runs-on: ubuntu-24.04",
        f"    timeout-minutes: {workflow.timeout_minutes}",
        "    outputs:",
        "      comments_total: ${{ steps.review.outputs.comments_total || steps.fallback_review.outputs.comments_total }}",
        "      comments_inline: ${{ steps.review.outputs.comments_inline || steps.fallback_review.outputs.comments_inline }}",
        "      comments_routed: ${{ steps.review.outputs.comments_routed || steps.fallback_review.outputs.comments_routed }}",
        "      comments_skipped: ${{ steps.review.outputs.comments_skipped || steps.fallback_review.outputs.comments_skipped }}",
        "      comments_failed: ${{ steps.review.outputs.comments_failed || steps.fallback_review.outputs.comments_failed }}",
        "      summary_comment_url: ${{ steps.review.outputs.summary_comment_url || steps.fallback_review.outputs.summary_comment_url }}",
        # The pinned npm launcher can otherwise replace itself during a review.
        "    env:",
        '      OCR_NO_UPDATE: "1"',
        "    steps:",
        "      - name: Check Review Run configuration",
        "        env:",
        "          OCR_TOKEN: ${{ secrets.OCR_LLM_AUTH_TOKEN }}",
        "          OCR_URL: ${{ vars.OCR_LLM_URL }}",
        "          OCR_MODEL: ${{ vars.OCR_LLM_MODEL }}",
        "          OCR_PROTOCOL: ${{ vars.OCR_LLM_USE_ANTHROPIC }}",
        "        shell: bash",
        "        run: |",
        '          if [ -z "$OCR_TOKEN" ]; then echo "::error::Missing Actions secret OCR_LLM_AUTH_TOKEN; add it in repository Settings > Secrets and variables > Actions."; exit 1; fi',
        '          if [ -z "$OCR_URL" ]; then echo "::error::Missing Actions variable OCR_LLM_URL; add it in repository Settings > Secrets and variables > Actions."; exit 1; fi',
        '          if [ -n "$OCR_PROTOCOL" ] && [ "$OCR_PROTOCOL" != "true" ] && [ "$OCR_PROTOCOL" != "false" ]; then echo "::error::Actions variable OCR_LLM_USE_ANTHROPIC must be true or false."; exit 1; fi',
        f"      - uses: alibaba/open-code-review@{workflow.action_sha} # v{workflow.ocr_version}",
        "        id: review",
        "        continue-on-error: true",
        "        with:",
        f"          ocr_version: {_yaml_string(workflow.ocr_version)}",
        "          llm_url: ${{ vars.OCR_LLM_URL }}",
        "          llm_auth_token: ${{ secrets.OCR_LLM_AUTH_TOKEN }}",
        "          llm_model: ${{ vars.OCR_LLM_MODEL || 'gemma-4-31b-it' }}",
        "          llm_use_anthropic: ${{ vars.OCR_LLM_USE_ANTHROPIC || 'false' }}",
        '          llm_extra_body: "{}"',
        f"          max_tokens_budget: {_yaml_string(str(workflow.max_tokens_budget))}",
        '          upload_artifacts: "false"',
        '          sticky_summary: "true"',
        '          incremental: "true"',
        '          resolve_outdated: "false"',
        f"      - name: Fallback Review (Gemini Flash Medium Effort)",
        "        if: steps.review.outcome == 'failure'",
        f"        uses: alibaba/open-code-review@{workflow.action_sha} # v{workflow.ocr_version}",
        "        id: fallback_review",
        "        with:",
        f"          ocr_version: {_yaml_string(workflow.ocr_version)}",
        "          llm_url: ${{ vars.OCR_LLM_URL }}",
        "          llm_auth_token: ${{ secrets.OCR_LLM_AUTH_TOKEN }}",
        "          llm_model: ${{ vars.OCR_FALLBACK_MODEL || 'gemini-3.8-flash' }}",
        "          llm_use_anthropic: ${{ vars.OCR_LLM_USE_ANTHROPIC }}",
        '          llm_extra_body: \'{"reasoning_effort": "medium"}\'',
        '          max_tokens_budget: "100000"',
        '          upload_artifacts: "false"',
        '          sticky_summary: "true"',
        '          incremental: "true"',
        '          resolve_outdated: "false"',
        "      - name: Verify Review Success",
        "        if: steps.review.outcome == 'failure' && steps.fallback_review.outcome != 'success'",
        "        run: |",
        '          echo "::error::Both primary review and fallback review failed."',
        "          exit 1",
        "  report:",
        "    needs: review",
        "    if: needs.review.result == 'success' && github.event.pull_request.head.repo.full_name == github.repository",
        "    runs-on: ubuntu-24.04",
        "    permissions:",
        "      contents: read",
        "      pull-requests: read",
        "      issues: write",
        "    steps:",
        "      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1",
        "        with:",
        "          ref: ${{ github.event.pull_request.base.sha }}",
        "          persist-credentials: false",
        "      - uses: actions/github-script@3a2844b7e9c422d3c10d287c895573f7108da1b3 # v7.0.8",
        "        id: report",
        "        env:",
        "          OCR_COMMENTS_TOTAL: ${{ needs.review.outputs.comments_total }}",
        "          OCR_COMMENTS_INLINE: ${{ needs.review.outputs.comments_inline }}",
        "          OCR_COMMENTS_ROUTED: ${{ needs.review.outputs.comments_routed }}",
        "          OCR_COMMENTS_SKIPPED: ${{ needs.review.outputs.comments_skipped }}",
        "          OCR_COMMENTS_FAILED: ${{ needs.review.outputs.comments_failed }}",
        "          OCR_SUMMARY_COMMENT_URL: ${{ needs.review.outputs.summary_comment_url }}",
        "        with:",
        "          script: |",
        "            const reporter = require('./scripts/review_issue_reporter.js');",
        "            await reporter.run({ github, context, core, counters: {",
        "              total: process.env.OCR_COMMENTS_TOTAL,",
        "              inline: process.env.OCR_COMMENTS_INLINE,",
        "              routed: process.env.OCR_COMMENTS_ROUTED,",
        "              skipped: process.env.OCR_COMMENTS_SKIPPED,",
        "              failed: process.env.OCR_COMMENTS_FAILED,",
        "              summaryUrl: process.env.OCR_SUMMARY_COMMENT_URL",
        "            } });",
    ]
    if workflow.rule_path is not None:
        lines.append("          rule: " + _yaml_string(workflow.rule_path))
    return ("\n".join(lines) + "\n").encode("utf-8")
