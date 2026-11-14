"""Interactive questionnaire stage for Dependabot configuration."""

from __future__ import annotations

from pathlib import Path
import sys
from typing import Optional, TYPE_CHECKING

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

if TYPE_CHECKING:
    try:
        from scripts.interactive import Session
    except ImportError:
        from interactive import Session

try:
    from scripts.prompts import prompt_confirm
except ImportError:
    from prompts import prompt_confirm

try:
    from scripts.dependabot_generator import detect_dependabot_ecosystems
except ImportError:
    from dependabot_generator import detect_dependabot_ecosystems


def prompt_dependabot_stage(session: Session, default: Optional[bool] = None) -> Optional[dict]:
    """Prompt user whether to configure Dependabot with restrained monthly updates and grouping.

    Returns:
        Constructed dependabot policy stage dictionary, or None if declined/interrupted.
    """
    effective_default = True if default is None else default
    session.out_stream.write(
        "\nDependabot keeps dependencies up to date. For solo maintainers, restrained monthly "
        "cadence and grouped updates prevent pull request noise while keeping security alerts immediate.\n"
    )
    session.out_stream.write(f"Recommended default: {'Yes' if effective_default else 'No'}.\n")
    session.out_stream.flush()

    confirmed = prompt_confirm(
        "Configure Dependabot (monthly cadence with grouped PRs)?",
        default=effective_default,
        in_stream=session.in_stream,
        out_stream=session.out_stream,
    )
    if not confirmed:
        return None

    ecosystems = detect_dependabot_ecosystems(
        api=getattr(session, "api", None),
        repository=getattr(session, "repository", None),
        inventory=getattr(session, "inventory", None),
    )
    return {
        "name": "dependabot",
        "cadence": "monthly",
        "package_ecosystems": ecosystems,
        "open_pull_requests_limit": 5,
        "groups": True,
    }
