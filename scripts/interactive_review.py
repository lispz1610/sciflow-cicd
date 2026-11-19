"""Interactive Review Setup prompts and managed workflow detection."""

import json
import re
import sys

try:
    from scripts.install_github_workflows import (_file, MARKER, REVIEW_MARKER,
                                                  REVIEW_RUNTIME_PATH, REVIEW_RUNTIME_MARKER,
                                                  render_review_reporter)
    from scripts.prompts import prompt_choice, prompt_confirm, prompt_multichoice, prompt_text, PromptExit
    from scripts.review_policy import parse_extra_body_json, validate_policy
    from scripts.review_workflow import WORKFLOW_PATH, render_workflow
except ImportError:
    from install_github_workflows import (_file, MARKER, REVIEW_MARKER,
                                          REVIEW_RUNTIME_PATH, REVIEW_RUNTIME_MARKER,
                                          render_review_reporter)
    from prompts import prompt_choice, prompt_confirm, prompt_multichoice, prompt_text, PromptExit
    from review_policy import parse_extra_body_json, validate_policy
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


_DEFAULT_SAMPLING_EXTRA_BODY = {"temperature": 0.2, "top_p": 0.95}
_CLEAR_EXTRA_BODY_SENTINELS = frozenset({"-", "none", "clear"})
_MODEL_SUGGESTION_RULES = (
    (("flash-thinking",), {"temperature": 0.7}),
    (("o1", "o3", "reasoning"), {"reasoning_effort": "medium"}),
)


def _parse_json_object(raw_value: str):
    """Parse a JSON string into a non-empty dict, or return None if invalid or empty."""
    if not isinstance(raw_value, str) or not raw_value.strip():
        return None
    try:
        parsed = parse_extra_body_json(raw_value)
    except (ValueError, TypeError):
        return None
    return parsed or None


def _extract_primary_extra_body(text: str):
    """Extract llm_extra_body only from the primary review step, never fallback_review."""
    primary_section = text.split("id: fallback_review", 1)[0]
    primary_section = primary_section.split("- name: Fallback Review", 1)[0]
    match = re.search(r"^\s*llm_extra_body:\s*(.+?)\s*$", primary_section, re.MULTILINE)
    if not match:
        return None
    raw_scalar = match.group(1)
    if raw_scalar.startswith("'") and raw_scalar.endswith("'") and len(raw_scalar) >= 2:
        raw_json = raw_scalar[1:-1].replace("''", "'")
        return _parse_json_object(raw_json)
    if raw_scalar.startswith('"') and raw_scalar.endswith('"') and len(raw_scalar) >= 2:
        direct = _parse_json_object(raw_scalar[1:-1])
        if direct is not None:
            return direct
        try:
            decoded = json.loads(raw_scalar)
            if isinstance(decoded, str):
                return _parse_json_object(decoded)
        except (ValueError, TypeError):
            return None
        return None
    return _parse_json_object(raw_scalar)


def _existing_defaults(content):
    text = content.decode("utf-8", errors="replace")
    def match(pattern, fallback):
        found = re.search(pattern, text)
        return found.group(1) if found else fallback
    runs_on_match = re.search(r"^\s*runs-on:\s*([^\n]+)", text, re.MULTILINE)
    multi_match = re.search(r"^\s*runs-on:\s*\n((?:\s*-\s*[^\n]+\n?)+)", text, re.MULTILINE)
    existing_runs_on = None
    if multi_match:
        existing_runs_on = [re.sub(r"^\s*-\s*", "", line).strip() for line in multi_match.group(1).splitlines() if line.strip()]
    elif runs_on_match:
        val = runs_on_match.group(1).strip()
        if val.startswith("[") and val.endswith("]"):
            existing_runs_on = [x.strip() for x in val[1:-1].split(",") if x.strip()]
        else:
            existing_runs_on = val.strip("'\"")
    return {
        "action_sha": match(r"alibaba/open-code-review@([0-9a-fA-F]{40})", DEFAULT_ACTION_SHA),
        "ocr_version": match(r'ocr_version: "([0-9.]+)"', DEFAULT_OCR_VERSION),
        "budget": match(r'max_tokens_budget: "([0-9]+)"', "50000"),
        "timeout": match(r"timeout-minutes: ([0-9]+)", "30"),
        "events": [event for event in EVENTS if re.search(r"types: \[([^]]+)\]", text) and event in re.search(r"types: \[([^]]+)\]", text).group(1)],
        "rule_path": match(r'          rule: "([^"]+)"', ""),
        "extra_body": _extract_primary_extra_body(text),
        "runs_on": existing_runs_on,
    }


def suggest_llm_extra_body(model: str, use_anthropic: bool):
    """Suggest sensible sampling and reasoning defaults for known models."""
    if not isinstance(model, str) or not model.strip():
        return None
    normalized_model = model.strip().lower()
    if (use_anthropic or "claude" in normalized_model) and any(
        token in normalized_model for token in ("claude-3-7", "claude-3.7", "thinking")
    ):
        return {"temperature": 1.0, "thinking": {"type": "enabled", "budget_tokens": 2048}}
    for tokens, preset in _MODEL_SUGGESTION_RULES:
        if any(token in normalized_model for token in tokens):
            return dict(preset)
    return dict(_DEFAULT_SAMPLING_EXTRA_BODY)


def _prompt_optional_number(message, default_val, parser, validator, error_msg, in_s, out_s):
    """Prompt for an optional numeric value; blank or clear sentinel returns None."""
    default_str = str(default_val) if default_val is not None else None
    prompt_msg = f"{message} ('-' to clear)" if default_str is not None else message
    while True:
        raw = prompt_text(prompt_msg, default=default_str, required=False, raise_on_interrupt=True,
                          in_stream=in_s, out_stream=out_s)
        stripped = raw.strip() if raw else ""
        if not stripped or stripped.lower() in _CLEAR_EXTRA_BODY_SENTINELS:
            return None
        try:
            parsed = parser(stripped)
        except ValueError:
            out_s.write(error_msg + "\n")
            continue
        if validator(parsed):
            return parsed
        out_s.write(error_msg + "\n")


def _prompt_individual_extra_body(existing_extra_body, in_s, out_s):
    """Guide the user through individual sampling and reasoning parameters."""
    current = existing_extra_body if isinstance(existing_extra_body, dict) else {}
    params = {}
    temp = _prompt_optional_number(
        "Temperature (0.0 to 2.0, blank to skip)",
        current.get("temperature"), float, lambda v: 0.0 <= v <= 2.0,
        "Temperature must be a number between 0.0 and 2.0.", in_s, out_s,
    )
    if temp is not None:
        params["temperature"] = temp
    top_p = _prompt_optional_number(
        "top_p (0.0 to 1.0, blank to skip)",
        current.get("top_p"), float, lambda v: 0.0 < v <= 1.0,
        "top_p must be a number > 0.0 and <= 1.0.", in_s, out_s,
    )
    if top_p is not None:
        params["top_p"] = top_p
    top_k = _prompt_optional_number(
        "top_k (positive integer, blank to skip)",
        current.get("top_k"), int, lambda v: v >= 1,
        "top_k must be an integer >= 1.", in_s, out_s,
    )
    if top_k is not None:
        params["top_k"] = top_k

    existing_thinking = current.get("thinking") if isinstance(current.get("thinking"), dict) else {}
    current_thinking_mode = existing_thinking.get("type")
    thinking_msg = (
        "Thinking mode (enabled / disabled, '-' to clear)"
        if current_thinking_mode is not None
        else "Thinking mode (enabled / disabled, blank to skip)"
    )
    while True:
        thinking_mode = prompt_text(
            thinking_msg,
            default=current_thinking_mode, required=False, raise_on_interrupt=True,
            in_stream=in_s, out_stream=out_s,
        )
        normalized_mode = thinking_mode.strip().lower() if thinking_mode else ""
        if not normalized_mode or normalized_mode in _CLEAR_EXTRA_BODY_SENTINELS:
            break
        if normalized_mode in ("enabled", "disabled"):
            thinking_obj = {"type": normalized_mode}
            if normalized_mode == "enabled":
                budget_tokens = _prompt_optional_number(
                    "Thinking budget_tokens (integer >= 1, default 2048)",
                    existing_thinking.get("budget_tokens", 2048), int, lambda v: v >= 1,
                    "budget_tokens must be an integer >= 1.", in_s, out_s,
                )
                if budget_tokens is not None:
                    thinking_obj["budget_tokens"] = budget_tokens
            params["thinking"] = thinking_obj
            break
        out_s.write("Thinking mode must be 'enabled', 'disabled', or blank.\n")

    current_effort = current.get("reasoning_effort")
    effort_msg = (
        "Reasoning effort (low / medium / high, '-' to clear)"
        if current_effort is not None
        else "Reasoning effort (low / medium / high, blank to skip)"
    )
    while True:
        effort = prompt_text(
            effort_msg,
            default=current_effort, required=False, raise_on_interrupt=True,
            in_stream=in_s, out_stream=out_s,
        )
        normalized_effort = effort.strip().lower() if effort else ""
        if not normalized_effort or normalized_effort in _CLEAR_EXTRA_BODY_SENTINELS:
            break
        if normalized_effort in ("low", "medium", "high"):
            params["reasoning_effort"] = normalized_effort
            break
        out_s.write("Reasoning effort must be 'low', 'medium', 'high', or blank.\n")

    return params or None


def _prompt_extra_body(model, anthropic, existing_extra_body, in_s, out_s):
    """Prompt the user to choose, customize, or enter llm_extra_body parameters."""
    suggested = suggest_llm_extra_body(model, anthropic)
    out_s.write(
        "LLM parameters control sampling and reasoning (temperature, top_p, top_k, thinking mode, effort) "
        "via llm_extra_body.\n"
    )
    options = []
    if existing_extra_body:
        options.append(f"Keep current parameters ({json.dumps(existing_extra_body, sort_keys=True)})")
    else:
        options.append("Use provider defaults (none)")
    if suggested:
        options.append(f"Use suggested defaults for {model} ({json.dumps(suggested, sort_keys=True)})")
    options.append("Configure individual parameters (temperature, top_p, top_k, thinking_mode, effort)")
    options.append("Enter raw llm_extra_body JSON")

    choice_idx = prompt_choice(
        "LLM parameter configuration", options, default=0, raise_on_interrupt=True,
        in_stream=in_s, out_stream=out_s,
    )
    chosen_label = options[choice_idx]
    if chosen_label.startswith("Keep current"):
        return existing_extra_body
    if chosen_label.startswith("Use provider defaults"):
        return None
    if chosen_label.startswith("Use suggested"):
        return dict(suggested)
    if chosen_label.startswith("Configure individual"):
        return _prompt_individual_extra_body(existing_extra_body, in_s, out_s)

    default_json = json.dumps(existing_extra_body or suggested, sort_keys=True) if (existing_extra_body or suggested) else None
    while True:
        raw = prompt_text(
            "LLM extra parameters JSON (blank for none)",
            default=default_json, required=False, raise_on_interrupt=True,
            in_stream=in_s, out_stream=out_s,
        )
        stripped = raw.strip() if raw else ""
        if not stripped or stripped == "{}":
            return None
        parsed = _parse_json_object(stripped)
        if parsed is not None:
            return parsed
        out_s.write("Invalid JSON object; enter a JSON object like '{\"temperature\": 0.2}' or leave blank.\n")
        default_json = None


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
    extra_body = _prompt_extra_body(model, anthropic, existing.get("extra_body"), in_s, out_s)
    out_s.write("Events choose when a Review Run starts: opened is a new PR, synchronize is a new commit (for example push to branch), and reopened is a reopened PR. Fork PRs are skipped. Recommended default: all three.\n")
    chosen = prompt_multichoice("Review Run events", list(EVENTS), default=[EVENTS.index(v) for v in existing.get("events", EVENTS)] or [0, 1, 2],
                                return_indices=True, required=True, raise_on_interrupt=True, in_stream=in_s, out_stream=out_s)
    out_s.write("Token budget caps tokens used by one Review Run; a lower budget limits cost and may reduce review coverage. Recommended default: 50000.\n")
    budget = _required_text("Maximum tokens per Review Run", existing.get("budget", "50000"), lambda v: v.isdigit() and 1 <= int(v) <= 1000000, in_s, out_s)
    out_s.write("Timeout ends a long Review Run; choose 1 to 120 minutes. Recommended default: 30 minutes.\n")
    timeout = _required_text("Review Run timeout in minutes", existing.get("timeout", "30"), lambda v: v.isdigit() and 1 <= int(v) <= 120, in_s, out_s)
    default_runner = getattr(session, "default_runner", None)
    if default_runner is None and session is not None:
        default_runner = (getattr(session, "defaults", {}) or {}).get("runner")
    if default_runner is None:
        default_runner = existing.get("runs_on") or "self-hosted"
    runner_display = ", ".join(default_runner) if isinstance(default_runner, (list, tuple)) else str(default_runner)

    out_s.write(
        f"Runner controls where Advisory Review and its issue reporter run. Recommended default: use default runner ({runner_display}).\n"
    )
    use_default_runner = prompt_confirm(
        f"Use default runner ({runner_display}) for Advisory Review? (Recommended: Yes)",
        default=True,
        raise_on_interrupt=raise_on_interrupt,
        in_stream=in_s,
        out_stream=out_s,
    )
    if use_default_runner is None:
        return None
    if use_default_runner:
        review_runs_on = default_runner
    else:
        use_self_hosted = prompt_confirm(
            "Use self-hosted runner for Advisory Review? (Recommended: Yes)",
            default=True,
            raise_on_interrupt=raise_on_interrupt,
            in_stream=in_s,
            out_stream=out_s,
        )
        if use_self_hosted is None:
            return None
        if use_self_hosted:
            custom_labels = prompt_text(
                "Runner label(s) for Advisory Review (comma-separated)",
                default="self-hosted",
                required=True,
                raise_on_interrupt=raise_on_interrupt,
                in_stream=in_s,
                out_stream=out_s,
            )
            if custom_labels is None:
                return None
            clist = [l.strip() for l in custom_labels.split(",") if l.strip()]
            review_runs_on = clist[0] if len(clist) == 1 else clist
        else:
            hosted_override = prompt_text(
                "Hosted runner for Advisory Review",
                default="ubuntu-24.04",
                required=True,
                raise_on_interrupt=raise_on_interrupt,
                in_stream=in_s,
                out_stream=out_s,
            )
            if hosted_override is None:
                return None
            review_runs_on = hosted_override.strip()
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
                "timeout_minutes": int(timeout), "runs_on": review_runs_on}
    if rule_path:
        workflow["rule_path"] = rule_path
    llm_dict = {"url": url, "model": model, "use_anthropic": anthropic, "secret_name": "OCR_LLM_AUTH_TOKEN"}
    if extra_body:
        llm_dict["extra_body"] = extra_body
    stage = {"name": "review", "workflow": workflow,
             "llm": llm_dict,
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
