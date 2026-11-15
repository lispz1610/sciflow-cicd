"""Interactive Review Setup prompts and managed workflow detection."""

import re
import sys

try:
    from scripts.install_github_workflows import (_file, MARKER, REVIEW_MARKER,
                                                  REVIEW_RUNTIME_PATH, REVIEW_RUNTIME_MARKER,
                                                  render_review_reporter)
    from scripts.prompts import prompt_confirm, prompt_multichoice, prompt_text, PromptExit
    from scripts.review_policy import validate_policy
    from scripts.review_workflow import WORKFLOW_PATH, render_workflow
except ImportError:
    from install_github_workflows import (_file, MARKER, REVIEW_MARKER,
                                          REVIEW_RUNTIME_PATH, REVIEW_RUNTIME_MARKER,
                                          render_review_reporter)
    from prompts import prompt_confirm, prompt_multichoice, prompt_text, PromptExit
    from review_policy import validate_policy
    from review_workflow import WORKFLOW_PATH, render_workflow

_ApiErrors = ()
try:
    import configure_github_repo
    _ApiErrors = _ApiErrors + (configure_github_repo.ApiError,)
except (ImportError, AttributeError):
    pass
try:
    import scripts.configure_github_repo
    _ApiErrors = _ApiErrors + (scripts.configure_github_repo.ApiError,)
except (ImportError, AttributeError):
    pass

DEFAULT_ACTION_SHA = "182898cf522da3d04157b422752d028417974e19"
DEFAULT_OCR_VERSION = "1.12.12"
EVENTS = ("opened", "synchronize", "reopened")


def detect_review_workflow(api, repository, default_branch):
    """Read the default branch and identify only the owned review template."""
    if not isinstance(default_branch, str) or not default_branch:
        return {"status": "unknown"}
    try:
        current = _file(api, "/repos/" + repository, WORKFLOW_PATH, default_branch)
    except (_ApiErrors + (OSError, ValueError)):
        return {"status": "unknown"}
    if current is None:
        return {"status": "absent"}
    content, sha = current
    if not content.startswith((REVIEW_MARKER.encode(),
                               (MARKER + "open-code-review version=1").encode(),
                               b"# github-repo-setup workflow-template=open-code-review version=1")):
        return {"status": "unmanaged", "sha": sha}
    return {"status": "managed", "sha": sha, "content": content}


def detect_review_reporter(api, repository, default_branch):
    """Read the separately owned reporter file before proposing an update."""
    if not isinstance(default_branch, str) or not default_branch:
        return {"status": "unknown"}
    try:
        current = _file(api, "/repos/" + repository, REVIEW_RUNTIME_PATH, default_branch)
    except (_ApiErrors + (OSError, ValueError)):
        return {"status": "unknown"}
    if current is None:
        return {"status": "absent"}
    content, sha = current
    if not content.startswith(REVIEW_RUNTIME_MARKER):
        return {"status": "unmanaged", "sha": sha}
    return {"status": "managed", "sha": sha, "content": content}


def _existing_defaults(content):
    text = content.decode("utf-8", errors="replace")
    def match(pattern, fallback):
        found = re.search(pattern, text)
        return found.group(1) if found else fallback
    return {
        "action_sha": match(r"alibaba/open-code-review@([0-9a-fA-F]{40})", DEFAULT_ACTION_SHA),
        "ocr_version": match(r'ocr_version: "([0-9.]+)"', DEFAULT_OCR_VERSION),
        "budget": match(r'max_tokens_budget: "([0-9]+)"', "50000"),
        "timeout": match(r"timeout-minutes: ([0-9]+)", "30"),
        "events": [event for event in EVENTS if re.search(r"types: \[([^]]+)\]", text) and event in re.search(r"types: \[([^]]+)\]", text).group(1)],
        "rule_path": match(r'          rule: "([^"]+)"', ""),
    }


def _required_text(message, default, validator, in_stream, out_stream):
    while True:
        value = prompt_text(message, default=default, required=True, raise_on_interrupt=True,
                            in_stream=in_stream, out_stream=out_stream)
        if validator(value):
            return value
        out_stream.write("Invalid value for this option; see its explanation and try again.\n")


def prompt_review_stage(session, raise_on_interrupt=False):
    """Return a validated optional review stage; never solicit a provider key."""
    in_s = session.in_stream or sys.stdin
    out_s = session.out_stream or sys.stdout
    visibility = session.repo_data.get("visibility", session.inventory.get("repository", {}).get("visibility"))
    state = getattr(session, "review_workflow", None)
    if not isinstance(state, dict):
        default_branch = session.repo_data.get("default_branch", session.inventory.get("repository", {}).get("default_branch"))
        state = detect_review_workflow(session.api, session.repository, default_branch)
    status = state.get("status", "unknown")
    out_s.write("\nReview Setup adds Advisory Review comments to eligible pull requests. It uses your provider account and may incur billing; PR content and findings can be exposed to the provider and in comments or logs.\n")
    out_s.write("It never approves a PR or becomes a required merge check. The workflow is proposed in the same draft Setup Pull Request as selected CI and dependency workflows.\n")
    out_s.write("The provider key is entered only in GitHub Settings > Secrets and variables > Actions as secret OCR_LLM_AUTH_TOKEN; do not enter it here. Variables OCR_LLM_URL, OCR_LLM_MODEL, and OCR_LLM_USE_ANTHROPIC are configured there too. Missing values can be fixed after setup.\n")
    out_s.write("When a successful review finds issues, a separate trusted reporter job creates or updates one managed GitHub Issue and links it from the pull request. Setup stages its trusted reporter script with the workflow. The target repository must have Issues enabled; that isolated job needs GITHUB_TOKEN permissions contents: read, issues: write, and pull-requests: read.\n")
    out_s.write(f"Current review workflow: {status}" + (f" (blob SHA {state['sha']})" if state.get("sha") else "") + ".\n")
    if visibility != "private":
        out_s.write("Review Setup currently supports private repositories only. Continue with other selected settings and workflows.\n")
        return None
    if status == "unmanaged":
        out_s.write("Conflict: this review workflow file is not managed by sciflow-cicd. Resolve it before choosing Review Setup; other settings can continue.\n")
        return None
    default_branch = session.repo_data.get("default_branch", session.inventory.get("repository", {}).get("default_branch"))
    reporter_state = detect_review_reporter(session.api, session.repository, default_branch)
    if reporter_state["status"] == "unmanaged":
        out_s.write(f"Conflict: {REVIEW_RUNTIME_PATH} is not managed by sciflow-cicd. Resolve it before choosing Review Setup.\n")
        return None
    default_on = bool(session.resume_mode and status == "managed")
    out_s.write(f"Yes stages Advisory Review for owner inspection; No leaves it unchanged. Recommended default: {'Yes' if default_on else 'No'}.\n")
    if not prompt_confirm("Stage Advisory Review?", default=default_on, raise_on_interrupt=raise_on_interrupt,
                          in_stream=in_s, out_stream=out_s):
        return None
    existing = _existing_defaults(state["content"]) if status == "managed" else {}
    def valid_url(value):
        candidate = {"name": "review", "workflow": {"template": "open-code-review", "template_version": 1,
             "filename": "open-code-review.yml", "triggers": ["pull_request_target"], "events": ["opened"],
             "fork_policy": "skip", "action_sha": DEFAULT_ACTION_SHA, "ocr_version": DEFAULT_OCR_VERSION,
             "max_tokens_budget": 1, "timeout_minutes": 1},
             "llm": {"url": value, "model": "example", "use_anthropic": False, "secret_name": "OCR_LLM_AUTH_TOKEN"},
             "limits": {"max_retention_days": 30}}
        try:
            validate_policy(candidate)
            return True
        except ValueError:
            return False
    out_s.write("Provider protocol controls the request format: Anthropic for Anthropic-compatible APIs (for example claude-3-5-sonnet); OpenAI for OpenAI-compatible APIs (for example gpt-4o-mini). Recommended default: OpenAI unless your provider says otherwise.\n")
    anthropic = prompt_confirm("Use Anthropic-compatible protocol?", default=False, raise_on_interrupt=True, in_stream=in_s, out_stream=out_s)
    out_s.write("Provider endpoint is the HTTPS API base URL, for example https://api.example.com/v1. It must not include credentials, query parameters, or a fragment.\n")
    url = _required_text("Provider HTTPS endpoint", None, valid_url, in_s, out_s)
    out_s.write("Model is the provider's exact model identifier, for example review-model-v1; it controls which model is billed.\n")
    model = _required_text("Provider model", None, lambda v: bool(v and len(v) <= 200), in_s, out_s)
    out_s.write("Events choose when a Review Run starts: opened is a new PR, synchronize is a new commit (for example push to branch), and reopened is a reopened PR. Fork PRs are skipped. Recommended default: all three.\n")
    chosen = prompt_multichoice("Review Run events", list(EVENTS), default=[EVENTS.index(v) for v in existing.get("events", EVENTS)] or [0, 1, 2],
                                return_indices=True, required=True, raise_on_interrupt=True, in_stream=in_s, out_stream=out_s)
    out_s.write("Token budget caps tokens used by one Review Run; a lower budget limits cost and may reduce review coverage. Recommended default: 50000.\n")
    budget = _required_text("Maximum tokens per Review Run", existing.get("budget", "50000"), lambda v: v.isdigit() and 1 <= int(v) <= 1000000, in_s, out_s)
    out_s.write("Timeout ends a long Review Run; choose 1 to 120 minutes. Recommended default: 30 minutes.\n")
    timeout = _required_text("Review Run timeout in minutes", existing.get("timeout", "30"), lambda v: v.isdigit() and 1 <= int(v) <= 120, in_s, out_s)
    action_sha = existing.get("action_sha", DEFAULT_ACTION_SHA)
    ocr_version = existing.get("ocr_version", DEFAULT_OCR_VERSION)
    retention = 30
    rule_path = existing.get("rule_path", "")
    out_s.write("Advanced options: exact Action and CLI pins control the code run by Actions. Update them only after verifying the releases; existing pins remain until you deliberately change them. Retention is a warning threshold; a trusted rule JSON path customizes review instructions. Recommended default: keep these values.\n")
    if prompt_confirm("Change advanced Review Setup options?", default=False, raise_on_interrupt=True, in_stream=in_s, out_stream=out_s):
        out_s.write("Action commit SHA must be an exact 40-character commit; staging verifies that the commit exists.\n")
        action_sha = _required_text("Review Action commit SHA", action_sha, lambda v: bool(re.fullmatch(r"[0-9a-fA-F]{40}", v)), in_s, out_s)
        out_s.write("CLI pin is an exact semantic version, at least 1.10.0; changing it changes the review runtime.\n")
        ocr_version = _required_text("Review CLI version", ocr_version, lambda v: bool(re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", v)) and tuple(map(int, v.split("."))) >= (1, 10, 0), in_s, out_s)
        out_s.write("Maximum retention is the allowed Actions log/artifact days; exceeding it warns without changing repository settings.\n")
        retention = int(_required_text("Maximum retention days", "30", lambda v: v.isdigit() and 1 <= int(v) <= 400, in_s, out_s))
        out_s.write("Trusted rule path is an optional JSON file under docs/ or .opencodereview/ on the default branch; leave blank to use defaults.\n")
        rule_path = prompt_text("Trusted rule JSON path (blank for none)", default=rule_path if rule_path else None,
                                raise_on_interrupt=True, in_stream=in_s, out_stream=out_s)
    workflow = {"template": "open-code-review", "template_version": 1, "filename": "open-code-review.yml",
                "triggers": ["pull_request_target"], "events": [EVENTS[i] for i in chosen], "fork_policy": "skip",
                "action_sha": action_sha, "ocr_version": ocr_version, "max_tokens_budget": int(budget),
                "timeout_minutes": int(timeout)}
    if rule_path:
        workflow["rule_path"] = rule_path
    stage = {"name": "review", "workflow": workflow,
             "llm": {"url": url, "model": model, "use_anthropic": anthropic, "secret_name": "OCR_LLM_AUTH_TOKEN"},
             "limits": {"max_retention_days": retention}}
    review = validate_policy(stage)
    if status == "managed" and render_workflow(review) != state["content"]:
        out_s.write(f"Managed Review Setup update: current blob SHA {state['sha']}. The staged workflow will replace this exact version after the owner merges the Setup Pull Request.\n")
        if not prompt_confirm("Confirm this managed workflow update?", default=False, raise_on_interrupt=True, in_stream=in_s, out_stream=out_s):
            out_s.write("Review Setup update skipped.\n")
            return None
        stage["workflow"]["expected_sha"] = state["sha"]
    if reporter_state["status"] == "managed" and render_review_reporter() != reporter_state["content"]:
        out_s.write(f"Managed Advisory Review reporter update: current blob SHA {reporter_state['sha']}. The staged script will replace this exact version after the owner merges the Setup Pull Request.\n")
        if not prompt_confirm("Confirm this managed reporter update?", default=False, raise_on_interrupt=True,
                              in_stream=in_s, out_stream=out_s):
            out_s.write("Review Setup update skipped.\n")
            return None
        stage["workflow"]["reporter_expected_sha"] = reporter_state["sha"]
    return stage
