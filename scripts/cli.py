"""CLI entrypoint and argument parser for sciflow-cicd."""

import argparse
from pathlib import Path
import sys
from typing import Callable, Dict, Optional, Sequence, TextIO

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

try:
    from scripts.interactive import init_interactive_session, run_state_detection, run_interactive_workflow
except ImportError:
    from interactive import init_interactive_session, run_state_detection, run_interactive_workflow

try:
    from scripts.headless import run_headless_apply
except ModuleNotFoundError:
    from headless import run_headless_apply


def _add_ocr_app_arguments(subparser: argparse.ArgumentParser) -> None:
    """Register shared limited GitHub App and allowlist arguments for OCR subcommands."""
    subparser.add_argument(
        "--app-id",
        required=True,
        help="Limited OCR GitHub App ID",
    )
    subparser.add_argument(
        "--private-key-path",
        required=True,
        metavar="PATH",
        help="Path to the PEM private key for the limited OCR GitHub App",
    )
    subparser.add_argument(
        "--allowlist",
        required=True,
        action="append",
        metavar="OWNER/REPO",
        help="Explicit repository allowlist (repeatable or comma-separated OWNER/REPO)",
    )
    subparser.add_argument(
        "--admin-app-id",
        action="append",
        default=[],
        metavar="ID",
        help="Administrative/client-setup GitHub App ID to explicitly reject (repeatable or comma-separated)",
    )
    subparser.add_argument(
        "--api-base-url",
        default="https://api.github.com",
        help="GitHub REST API base URL (default: https://api.github.com)",
    )


def _add_ocr_eligibility_arguments(subparser: argparse.ArgumentParser) -> None:
    """Register shared PR candidate eligibility arguments for OCR subcommands."""
    subparser.add_argument(
        "--eligibility-mode",
        choices=["either", "label", "maintainer"],
        default="either",
        help="Candidate PR eligibility rule (default: either)",
    )
    subparser.add_argument(
        "--eligible-label",
        default="eligible for review",
        help="Label marking a pull request as eligible for review (default: 'eligible for review')",
    )
    subparser.add_argument(
        "--maintainer",
        action="append",
        default=[],
        metavar="LOGIN",
        help="Optional explicit maintainer login allowlist (repeatable or comma-separated)",
    )
    subparser.add_argument(
        "--include-drafts",
        action="store_true",
        help="Include draft pull requests as candidates",
    )


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for sciflow-cicd."""
    parser = argparse.ArgumentParser(
        prog="sciflow-cicd",
        description="Interactive and policy-driven GitHub repository configuration tool for scientific repositories.",
    )
    subparsers = parser.add_subparsers(
        dest="command",
        title="available commands",
        metavar="<command>",
    )

    dry_run_parser = subparsers.add_parser(
        "dry-run",
        help="Inspect Inventory and proposed Policy changes without writes",
    )
    dry_run_parser.add_argument("--policy", required=True, metavar="PATH")
    dry_run_parser.add_argument("--repo", default=None, metavar="OWNER/REPO")

    setup_parser = subparsers.add_parser(
        "setup",
        help="Run interactive repository setup session",
        description="Run guided interactive repository setup session.",
    )

    doctor_parser = subparsers.add_parser(
        "doctor",
        help="Check runtime dependencies, environment health, or repository controls",
        description="Verify environment health or audit a repository's security and merge controls against desired baselines.",
    )
    doctor_parser.add_argument("--repo", help="Target repository OWNER/REPO to audit")
    doctor_parser.add_argument("--policy", help="Path to policy JSON document for repository audit")
    doctor_parser.add_argument("--json", action="store_true", help="Output findings as structured JSON")
    doctor_parser.add_argument("--token-env", default="GITHUB_TOKEN", help="Environment variable for GitHub token")

    apply_parser = subparsers.add_parser(
        "apply",
        help="Apply a policy file in headless mode",
        description="Apply repository configuration declared in a policy file without interactive prompts.",
    )
    apply_parser.add_argument(
        "--policy",
        required=True,
        metavar="PATH",
        help="Path to the policy JSON document to apply",
    )
    apply_parser.add_argument(
        "--repo",
        default=None,
        metavar="OWNER/REPO",
        help="Target GitHub repository in OWNER/REPO format",
    )
    apply_parser.add_argument(
        "--dry-run-result", metavar="PATH", dest="dry_run_result",
        help="JSON result saved from sciflow-cicd dry-run for this Policy and repository",
    )
    apply_parser.add_argument("--plan", metavar="PATH", dest="dry_run_result",
                              help=argparse.SUPPRESS)

    verify_parser = subparsers.add_parser(
        "verify-release",
        help="Verify release candidate evidence, CI status, and report consistency",
        description="Verify release candidate identity, CI jobs, live validation evidence, and release report.",
    )
    try:
        from scripts.verify_release import add_release_verification_arguments
    except ImportError:
        from verify_release import add_release_verification_arguments
    add_release_verification_arguments(verify_parser)

    ocr_parser = subparsers.add_parser(
        "ocr",
        help="Automated Open Code Review (OCR) preflight and operations",
        description="Automated Open Code Review (OCR) credential, scope, and review operations.",
    )
    ocr_subparsers = ocr_parser.add_subparsers(
        dest="ocr_command",
        title="available ocr commands",
        metavar="<ocr-command>",
    )
    preflight_app_parser = ocr_subparsers.add_parser(
        "preflight-app",
        help="Validate limited OCR GitHub App credentials, permissions, and repository allowlist",
        description="Preflight limited OCR GitHub App identity, installation scope, and repository permissions.",
    )
    _add_ocr_app_arguments(preflight_app_parser)
    preflight_app_parser.add_argument(
        "--json",
        action="store_true",
        help="Output findings as structured JSON",
    )

    preflight_parser = ocr_subparsers.add_parser(
        "preflight",
        help="Preflight OCR runtime executable, provider settings, App scope, and competing workflows",
        description=(
            "Verify OCR executable availability, LLM provider configuration, limited App PR-comment "
            "permissions, PR scoping, and competing Review Setup workflows."
        ),
    )
    _add_ocr_app_arguments(preflight_parser)
    preflight_parser.add_argument(
        "--ocr-bin",
        default=None,
        metavar="PATH_OR_CMD",
        help="Optional OCR executable name or path (defaults to OCR_CLI_BIN or 'ocr')",
    )
    _add_ocr_eligibility_arguments(preflight_parser)
    preflight_parser.add_argument(
        "--inspect-prs",
        action="store_true",
        help="Inspect open pull requests in allowlisted repositories to verify PR scoping",
    )
    preflight_parser.add_argument(
        "--json",
        action="store_true",
        help="Output findings as structured JSON",
    )

    poll_parser = ocr_subparsers.add_parser(
        "poll",
        help="Poll allowlisted repositories for eligible Advisory Review pull requests",
        description="Poll allowlisted repositories covered by the limited OCR GitHub App with fair rotation and backoff.",
    )
    _add_ocr_app_arguments(poll_parser)
    _add_ocr_eligibility_arguments(poll_parser)
    poll_parser.add_argument(
        "--interval",
        type=float,
        default=60.0,
        help="Bounded polling interval in seconds between cycles (clamped to [5, 3600], default: 60)",
    )
    poll_parser.add_argument(
        "--max-cycles",
        type=int,
        default=1,
        help="Number of polling cycles to run (default: 1)",
    )
    poll_parser.add_argument(
        "--max-repos-per-cycle",
        type=int,
        default=None,
        help="Maximum allowlisted repositories to poll per cycle (round-robin)",
    )
    poll_parser.add_argument(
        "--cursor",
        type=int,
        default=None,
        help="Starting round-robin repository index",
    )
    poll_parser.add_argument(
        "--state-path",
        default=None,
        metavar="PATH",
        help="Optional JSON file path to persist round-robin rotation cursor",
    )
    poll_parser.add_argument(
        "--lock-path",
        default=None,
        metavar="PATH",
        help="Optional lockfile path for single-flight active evaluation guard",
    )
    poll_parser.add_argument(
        "--json",
        action="store_true",
        help="Output polling results as structured JSON",
    )

    safety_parser = ocr_subparsers.add_parser(
        "check-safety",
        help="Enforce strict fail-closed fork safety and non-self-hosted runner boundaries",
        description=(
            "Inspect open pull requests discovered in allowlisted repositories through the "
            "fail-closed same-repository origin boundary and verify local direct execution mode."
        ),
    )
    _add_ocr_app_arguments(safety_parser)
    safety_parser.add_argument(
        "--execution-mode",
        default="local_direct",
        help="OCR execution mode to verify against runner boundary (default: local_direct)",
    )
    safety_parser.add_argument(
        "--json",
        action="store_true",
        help="Output safety decisions as structured JSON",
    )

    run_parser = ocr_subparsers.add_parser(
        "run",
        help="Run a single-pass advisory OCR review across allowlisted repositories",
        description=(
            "Preflight runtime/App/workflows, poll allowlisted same-repository pull requests, "
            "enforce fork safety, execute OCR once per HEAD SHA, and post strictly advisory feedback."
        ),
    )
    _add_ocr_app_arguments(run_parser)
    run_parser.add_argument(
        "--state-path",
        default=None,
        metavar="PATH",
        help=(
            "Optional JSON file path to persist rotation cursor and per-HEAD review state "
            "(defaults to a per-App ID state file in the system temp directory)"
        ),
    )
    run_parser.add_argument(
        "--lock-path",
        default=None,
        metavar="PATH",
        help="Optional lockfile path for single-flight active evaluation guard",
    )
    run_parser.add_argument(
        "--ocr-bin",
        default=None,
        metavar="PATH_OR_CMD",
        help="Optional OCR executable name or path (defaults to OCR_CLI_BIN or 'ocr')",
    )
    _add_ocr_eligibility_arguments(run_parser)
    run_parser.add_argument(
        "--review-mode",
        default="pr_review",
        help="Strictly advisory publication mode: 'pr_review' (COMMENT review) or 'issue_comment'",
    )
    run_parser.add_argument(
        "--max-reviews",
        type=int,
        default=None,
        help="Maximum number of unreviewed PR candidates to evaluate in this pass",
    )
    run_parser.add_argument(
        "--interval",
        type=float,
        default=60.0,
        help="Bounded polling interval in seconds between cycles (clamped to [5, 3600], default: 60)",
    )
    run_parser.add_argument(
        "--max-cycles",
        type=int,
        default=1,
        help="Number of bounded review polling cycles to run (default: 1)",
    )
    run_parser.add_argument(
        "--json",
        action="store_true",
        help="Output advisory review pass results as structured JSON",
    )

    return parser


def run_interactive(
    in_stream: Optional[TextIO] = None,
    out_stream: Optional[TextIO] = None,
    err_stream: Optional[TextIO] = None,
) -> int:
    """Entry point for interactive setup session."""
    out_s = out_stream if out_stream is not None else sys.stdout
    out_s.write("Starting interactive setup session...\n")
    out_s.flush()

    session = init_interactive_session(
        in_stream=in_stream,
        out_stream=out_stream,
        err_stream=err_stream,
    )
    if session is None:
        return 1

    if not run_state_detection(session):
        return 1

    return run_interactive_workflow(session)


def _dispatch_ocr_preflight_app(args: argparse.Namespace) -> int:
    try:
        from scripts.ocr_app_auth import run_app_preflight
    except ImportError:
        from ocr_app_auth import run_app_preflight
    return run_app_preflight(
        app_id=args.app_id,
        private_key_path=args.private_key_path,
        allowlist=args.allowlist,
        admin_app_ids=args.admin_app_id,
        api_base_url=args.api_base_url,
        json_output=args.json,
    )


def _dispatch_ocr_preflight(args: argparse.Namespace) -> int:
    try:
        from scripts.ocr_preflight import run_ocr_preflight
    except ImportError:
        from ocr_preflight import run_ocr_preflight
    return run_ocr_preflight(
        app_id=args.app_id,
        private_key_path=args.private_key_path,
        allowlist=args.allowlist,
        admin_app_ids=args.admin_app_id,
        api_base_url=args.api_base_url,
        ocr_bin=args.ocr_bin,
        eligibility_mode=args.eligibility_mode,
        eligible_label=args.eligible_label,
        maintainers=args.maintainer,
        include_drafts=args.include_drafts,
        inspect_pull_requests=args.inspect_prs,
        json_output=args.json,
    )


def _dispatch_ocr_poll(args: argparse.Namespace) -> int:
    try:
        from scripts.ocr_poller import run_ocr_poll
    except ImportError:
        from ocr_poller import run_ocr_poll
    return run_ocr_poll(
        app_id=args.app_id,
        private_key_path=args.private_key_path,
        allowlist=args.allowlist,
        admin_app_ids=args.admin_app_id,
        api_base_url=args.api_base_url,
        eligibility_mode=args.eligibility_mode,
        eligible_label=args.eligible_label,
        maintainers=args.maintainer,
        include_drafts=args.include_drafts,
        interval_seconds=args.interval,
        max_cycles=args.max_cycles,
        max_repos_per_cycle=args.max_repos_per_cycle,
        cursor_index=args.cursor,
        state_path=args.state_path,
        lock_path=args.lock_path,
        json_output=args.json,
    )


def _dispatch_ocr_check_safety(args: argparse.Namespace) -> int:
    try:
        from scripts.ocr_poller import discover_repository_pull_requests
        from scripts.ocr_safety import run_ocr_safety_check
    except ImportError:
        from ocr_poller import discover_repository_pull_requests
        from ocr_safety import run_ocr_safety_check
    return run_ocr_safety_check(
        app_id=args.app_id,
        private_key_path=args.private_key_path,
        allowlist=args.allowlist,
        admin_app_ids=args.admin_app_id,
        api_base_url=args.api_base_url,
        execution_mode=args.execution_mode,
        json_output=args.json,
        pr_fetcher=discover_repository_pull_requests,
    )


def _dispatch_ocr_run(args: argparse.Namespace) -> int:
    try:
        from scripts.ocr_reviewer import run_ocr_reviews
    except ImportError:
        from ocr_reviewer import run_ocr_reviews
    return run_ocr_reviews(
        app_id=args.app_id,
        private_key_path=args.private_key_path,
        allowlist=args.allowlist,
        admin_app_ids=args.admin_app_id,
        api_base_url=args.api_base_url,
        state_path=args.state_path,
        lock_path=args.lock_path,
        ocr_bin=args.ocr_bin,
        eligibility_mode=args.eligibility_mode,
        eligible_label=args.eligible_label,
        maintainers=args.maintainer,
        include_drafts=args.include_drafts,
        review_mode=args.review_mode,
        max_reviews=args.max_reviews,
        interval_seconds=args.interval,
        max_cycles=args.max_cycles,
        json_output=args.json,
    )


_OCR_COMMAND_HANDLERS: Dict[str, Callable[[argparse.Namespace], int]] = {
    "preflight-app": _dispatch_ocr_preflight_app,
    "preflight": _dispatch_ocr_preflight,
    "poll": _dispatch_ocr_poll,
    "check-safety": _dispatch_ocr_check_safety,
    "run": _dispatch_ocr_run,
}


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Main CLI entrypoint."""
    if argv is None:
        argv = sys.argv[1:]

    parser = build_parser()

    # When invoked with no arguments, route to interactive setup session
    if not argv:
        return run_interactive()

    args = parser.parse_args(argv)

    if args.command == "setup":
        return run_interactive()

    if args.command == "doctor":
        try:
            from scripts.doctor import run_doctor
        except ImportError:
            from doctor import run_doctor
        return run_doctor(
            repo=getattr(args, "repo", None),
            policy_path=getattr(args, "policy", None),
            json_output=getattr(args, "json", False),
            token_env=getattr(args, "token_env", "GITHUB_TOKEN"),
        )

    if args.command == "apply":
        return run_headless_apply(policy_path=args.policy, repo=args.repo,
                                  plan_path=args.dry_run_result)

    if args.command == "dry-run":
        return run_headless_apply(policy_path=args.policy, repo=args.repo, apply=False)

    if args.command == "verify-release":
        try:
            from scripts.verify_release import run_verify_release
        except ImportError:
            from verify_release import run_verify_release
        return run_verify_release(
            record_path=args.record,
            report_path=args.report,
            repo=args.repo,
            token=args.token,
            output_format=args.format,
            ci_mode=args.ci,
            record_ref=args.record_ref,
            labels=args.labels,
        )

    if args.command == "ocr":
        handler = _OCR_COMMAND_HANDLERS.get(getattr(args, "ocr_command", None))
        if handler is not None:
            return handler(args)
        parser.parse_args(["ocr", "--help"])
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
