"""Terminal interaction helpers using Python standard library."""

import sys
from typing import Optional, Sequence, Union, List, TextIO


class PromptExit(Exception):
    """Raised when user cancels or aborts interactive input."""
    pass


def _read_line(
    prompt_str: str,
    raise_on_interrupt: bool = False,
    in_stream: Optional[TextIO] = None,
    out_stream: Optional[TextIO] = None,
) -> Optional[str]:
    in_s = in_stream if in_stream is not None else sys.stdin
    out_s = out_stream if out_stream is not None else sys.stdout

    try:
        out_s.write(prompt_str)
        out_s.flush()
        line = in_s.readline()
        if line == "":
            if raise_on_interrupt:
                raise PromptExit("EOF encountered")
            return None
        return line.rstrip("\r\n")
    except (EOFError, KeyboardInterrupt):
        if raise_on_interrupt:
            raise PromptExit("Prompt interrupted")
        return None


def prompt_text(
    message: str,
    default: Optional[str] = None,
    required: bool = False,
    raise_on_interrupt: bool = False,
    in_stream: Optional[TextIO] = None,
    out_stream: Optional[TextIO] = None,
) -> Optional[str]:
    """Prompt the user for a text input.

    Args:
        message: The prompt description shown to the user.
        default: Optional default value used if user enters blank input.
        required: If True, reprompts until non-empty input is received.
        raise_on_interrupt: If True, raises PromptExit instead of returning None.
        in_stream: Input stream, defaults to sys.stdin.
        out_stream: Output stream, defaults to sys.stdout.

    Returns:
        The entered string (stripped), the default value, or None on EOF/interrupt.
    """
    out_s = out_stream if out_stream is not None else sys.stdout
    prompt_str = f"{message} [{default}]: " if default is not None else f"{message}: "

    while True:
        line = _read_line(
            prompt_str,
            raise_on_interrupt=raise_on_interrupt,
            in_stream=in_stream,
            out_stream=out_stream,
        )
        if line is None:
            return None
        stripped = line.strip()
        if not stripped:
            if default is not None:
                return default
            if required:
                out_s.write("This field is required. Please enter a value.\n")
                out_s.flush()
                continue
            return ""
        return stripped


def prompt_choice(
    message: str,
    options: Sequence[str],
    default: Optional[int] = None,
    raise_on_interrupt: bool = False,
    in_stream: Optional[TextIO] = None,
    out_stream: Optional[TextIO] = None,
) -> Optional[int]:
    """Prompt the user to select one option from a numbered list.

    Args:
        message: The prompt description shown to the user.
        options: Sequence of string options to display.
        default: Optional 0-based default selection index.
        raise_on_interrupt: If True, raises PromptExit instead of returning None.
        in_stream: Input stream, defaults to sys.stdin.
        out_stream: Output stream, defaults to sys.stdout.

    Returns:
        The 0-based index of the chosen option, or None on EOF/interrupt.
    """
    if not options:
        raise ValueError("options must not be empty")

    if default is not None and not (0 <= default < len(options)):
        raise ValueError(f"default index {default} out of range (0-{len(options)-1})")

    out_s = out_stream if out_stream is not None else sys.stdout

    out_s.write(f"{message}\n")
    for i, opt in enumerate(options, 1):
        out_s.write(f"  {i}. {opt}\n")

    if default is not None:
        prompt_str = f"Enter selection [1-{len(options)}] [{default + 1}]: "
    else:
        prompt_str = f"Enter selection [1-{len(options)}]: "

    while True:
        line = _read_line(
            prompt_str,
            raise_on_interrupt=raise_on_interrupt,
            in_stream=in_stream,
            out_stream=out_stream,
        )
        if line is None:
            return None
        stripped = line.strip()
        if not stripped:
            if default is not None:
                return default
            out_s.write(
                f"Invalid selection. Please enter a number between 1 and {len(options)}.\n"
            )
            out_s.flush()
            continue

        try:
            val = int(stripped)
            if 1 <= val <= len(options):
                return val - 1
            out_s.write(
                f"Invalid selection: {val}. Please enter a number between 1 and {len(options)}.\n"
            )
            out_s.flush()
        except ValueError:
            out_s.write(
                f"Invalid selection: '{stripped}'. Please enter a number between 1 and {len(options)}.\n"
            )
            out_s.flush()


def prompt_multichoice(
    message: str,
    options: Sequence[str],
    default: Optional[Sequence[Union[int, str]]] = None,
    return_indices: bool = False,
    required: bool = False,
    raise_on_interrupt: bool = False,
    in_stream: Optional[TextIO] = None,
    out_stream: Optional[TextIO] = None,
) -> Optional[Union[List[str], List[int]]]:
    """Prompt the user to select multiple options from a numbered list.

    Args:
        message: The prompt description shown to the user.
        options: Sequence of string options to display.
        default: Optional pre-selected indices (int) or item names (str).
        return_indices: If True, returns list of 0-based indices; else list of strings.
        required: If True, requires at least one selection.
        raise_on_interrupt: If True, raises PromptExit instead of returning None.
        in_stream: Input stream, defaults to sys.stdin.
        out_stream: Output stream, defaults to sys.stdout.

    Returns:
        List of selected option strings or 0-based indices, or None on EOF/interrupt.
    """
    if not options:
        raise ValueError("options must not be empty")

    default_indices: Optional[set[int]] = None
    if default is not None:
        default_indices = set()
        for item in default:
            if isinstance(item, int):
                if not (0 <= item < len(options)):
                    raise ValueError(
                        f"default index {item} out of range (0-{len(options)-1})"
                    )
                default_indices.add(item)
            elif isinstance(item, str):
                if item in options:
                    default_indices.add(options.index(item))
                else:
                    raise ValueError(f"default item '{item}' not found in options")
            else:
                raise ValueError(f"invalid default element: {item}")

    out_s = out_stream if out_stream is not None else sys.stdout

    out_s.write(f"{message}\n")
    for i, opt in enumerate(options, 1):
        is_selected = default_indices is not None and (i - 1) in default_indices
        box = "[x]" if is_selected else "[ ]"
        out_s.write(f"  {box} {i}. {opt}\n")

    if default_indices is not None:
        if default_indices:
            def_str = ", ".join(str(idx + 1) for idx in sorted(default_indices))
        else:
            def_str = "none"
        prompt_str = f"Enter choices (comma-separated) [{def_str}]: "
    else:
        prompt_str = "Enter choices (comma-separated): "

    while True:
        line = _read_line(
            prompt_str,
            raise_on_interrupt=raise_on_interrupt,
            in_stream=in_stream,
            out_stream=out_stream,
        )
        if line is None:
            return None
        stripped = line.strip()
        if not stripped:
            if default_indices is not None:
                selected_indices = sorted(default_indices)
                if return_indices:
                    return selected_indices
                return [options[i] for i in selected_indices]
            if required:
                out_s.write("Please select at least one option.\n")
                out_s.flush()
                continue
            return [] if not return_indices else []

        if stripped.lower() in ("none", "0"):
            if required:
                out_s.write("Please select at least one option.\n")
                out_s.flush()
                continue
            return [] if not return_indices else []

        tokens = [t.strip() for t in stripped.split(",") if t.strip()]
        if not tokens:
            continue

        selected = set()
        valid = True
        for token in tokens:
            try:
                val = int(token)
                if 1 <= val <= len(options):
                    selected.add(val - 1)
                else:
                    out_s.write(
                        f"Invalid selection: {val}. Please enter numbers between 1 and {len(options)}.\n"
                    )
                    out_s.flush()
                    valid = False
                    break
            except ValueError:
                out_s.write(
                    f"Invalid selection: '{token}'. Please enter numbers between 1 and {len(options)}.\n"
                )
                out_s.flush()
                valid = False
                break

        if not valid:
            continue

        if required and not selected:
            out_s.write("Please select at least one option.\n")
            out_s.flush()
            continue

        result_indices = sorted(selected)
        if return_indices:
            return result_indices
        return [options[i] for i in result_indices]


def prompt_confirm(
    message: str,
    default: bool = False,
    raise_on_interrupt: bool = False,
    in_stream: Optional[TextIO] = None,
    out_stream: Optional[TextIO] = None,
) -> Optional[bool]:
    """Prompt the user for a boolean yes/no confirmation.

    Args:
        message: The prompt description shown to the user.
        default: Default boolean returned on empty input. True displays [Y/n], False displays [y/N].
        raise_on_interrupt: If True, raises PromptExit instead of returning None.
        in_stream: Input stream, defaults to sys.stdin.
        out_stream: Output stream, defaults to sys.stdout.

    Returns:
        True/False boolean, or None on EOF/interrupt.
    """
    out_s = out_stream if out_stream is not None else sys.stdout
    indicator = "[Y/n]" if default else "[y/N]"
    prompt_str = f"{message} {indicator}: "

    while True:
        line = _read_line(
            prompt_str,
            raise_on_interrupt=raise_on_interrupt,
            in_stream=in_stream,
            out_stream=out_stream,
        )
        if line is None:
            return None
        stripped = line.strip().lower()
        if not stripped:
            return default
        if stripped in ("y", "yes"):
            return True
        if stripped in ("n", "no"):
            return False
        out_s.write("Please enter 'y' or 'n'.\n")
        out_s.flush()
