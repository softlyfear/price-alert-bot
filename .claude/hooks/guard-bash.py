"""PreToolUse guard for Bash: block system-wiping commands, ask before destructive ones.

`sudo` and `rm` stay allowed in general; only the catastrophic forms are denied
(system and home roots, the project root and its ancestors, `.claude`), and a short
list of data-destroying operations asks the user first.
"""

import json
import os
import re
import shlex
import sys

SYSTEM_DIRS = {
    "/",
    "/*",
    "/bin",
    "/boot",
    "/dev",
    "/etc",
    "/home",
    "/lib",
    "/lib64",
    "/opt",
    "/proc",
    "/root",
    "/sbin",
    "/srv",
    "/sys",
    "/usr",
    "/var",
}
HOME_TARGETS = {
    "~",
    "~/",
    "~/*",
    "$HOME",
    "$HOME/",
    "$HOME/*",
    "${HOME}",
    "${HOME}/",
    "${HOME}/*",
}
SEGMENT_SPLIT = re.compile(r"\|\||&&|;|\||\n|&(?!>)")
WRAPPERS = {"sudo", "doas", "env", "command", "nice", "nohup", "time", "exec"}
SUDO_ARG_OPTS = {"-u", "-g", "-C", "-D", "-h", "-p", "-r", "-t", "-U", "-T"}
ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*")
HOME = os.path.expanduser("~").rstrip("/")


def _tokens(segment: str) -> list[str]:
    try:
        words = shlex.split(segment, posix=True)
    except ValueError:
        words = segment.split()
    # Drop leading wrappers with their options (sudo -E -u root, env -i VAR=1, ...).
    while words and (words[0] in WRAPPERS or ASSIGNMENT.fullmatch(words[0])):
        wrapper, words = words[0], words[1:]
        while words and (words[0].startswith("-") or ASSIGNMENT.fullmatch(words[0])):
            takes_arg = wrapper in ("sudo", "doas") and words[0] in SUDO_ARG_OPTS
            words = words[2:] if takes_arg else words[1:]
    return words


def _is_recursive(flags: list[str]) -> bool:
    for f in flags:
        if f in ("--recursive",) or (
            f.startswith("-") and not f.startswith("--") and ("r" in f or "R" in f)
        ):
            return True
    return False


def _norm(path: str) -> str:
    if path == "/*":
        return path
    return path.rstrip("/") or "/"


def _wipes_project(target: str, cwd: str, project: str) -> bool:
    """True when the target is the project root, one of its ancestors or `.claude`.

    `.claude` holds unversioned pipeline documents, so it is guarded like the root.
    """
    wipes_contents = target == "*" or target.endswith("/*")
    base = target[:-1] if wipes_contents else target
    path = os.path.normpath(os.path.join(cwd, os.path.expanduser(base or ".")))
    guarded = (project, os.path.join(project, ".claude"))
    return path in guarded or project.startswith(path.rstrip("/") + "/")


def check(
    command: str, cwd: str | None = None, project: str | None = None
) -> tuple[str, str] | None:
    """Return (decision, reason) or None when the command is fine."""
    cwd = cwd or os.getcwd()
    project = os.path.normpath(project or os.environ.get("CLAUDE_PROJECT_DIR", cwd))
    if re.search(r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:", command):
        return "deny", "fork bomb"
    for raw in SEGMENT_SPLIT.split(command):
        words = _tokens(raw.strip())
        if not words:
            continue
        prog, args = words[0].rsplit("/", 1)[-1], words[1:]
        flags = [a for a in args if a.startswith("-")]
        targets = [a for a in args if not a.startswith("-")]

        if prog == "rm":
            if "--no-preserve-root" in flags:
                return "deny", "rm --no-preserve-root"
            for t in targets:
                n = _norm(t)
                if n.startswith("/dev/") or n == "/dev":
                    return "deny", f"rm on device path {t}"
                if _is_recursive(flags) and (
                    n in SYSTEM_DIRS or t in HOME_TARGETS or n in (HOME, HOME + "/*")
                ):
                    return "deny", f"recursive rm of system or home root {t}"
                if (
                    _is_recursive(flags)
                    and re.fullmatch(r"(/[^/]+){1,2}/?\*?", t)
                    and n.split("/")[1]
                    in {d.strip("/") for d in SYSTEM_DIRS if d not in ("/", "/*")}
                    and not n.startswith(("/tmp", "/home/"))
                ):
                    return "deny", f"recursive rm inside system dir {t}"
                if _is_recursive(flags) and _wipes_project(t, cwd, project):
                    return "deny", f"recursive rm of the project or .claude {t}"
                if _is_recursive(flags) and re.search(r"(^|/)\.git/?$", t):
                    return "ask", f"recursive rm of git metadata {t}"
        elif prog.startswith("mkfs") or prog in ("wipefs", "fdisk", "sfdisk", "parted"):
            return "deny", f"{prog} rewrites a disk"
        elif prog == "dd" and any(
            a.startswith("of=/dev/") and a != "of=/dev/null" for a in args
        ):
            return "deny", "dd writing to a device"
        elif prog == "shred" and any(t.startswith("/dev/") for t in targets):
            return "deny", "shred on a device"
        elif (
            prog in ("chmod", "chown", "chgrp")
            and _is_recursive(flags)
            and any(_norm(t) in SYSTEM_DIRS for t in targets)
        ):
            return "deny", f"recursive {prog} on a system root"
        elif prog == "docker":
            joined = " ".join(args)
            if re.search(r"\bsystem prune\b|\bvolume (rm|prune)\b", joined):
                return "ask", f"docker {joined}: deletes data"
            if re.search(r"\bcompose\b.*\bdown\b", joined) and re.search(
                r"(^|\s)(-v|--volumes)(\s|$)", joined
            ):
                return "ask", "docker compose down -v deletes database volumes"
        elif prog == "git":
            joined = " ".join(args)
            if re.search(r"\bpush\b", joined) and re.search(
                r"(^|\s)(-f|--force|--force-with-lease\S*)(\s|$)|\s\+\S", joined
            ):
                return "ask", "force push rewrites remote history"
            if re.search(r"\breset\b.*--hard", joined):
                return "ask", "git reset --hard discards uncommitted work"
            if re.search(r"\bclean\b", joined) and re.search(r"(^|\s)-\w*f", joined):
                return "ask", "git clean -f deletes untracked files"
            if re.search(r"\b(checkout|restore)\b.*(\s--\s+\.|\s\.$)", joined):
                return "ask", "discards uncommitted changes in the working tree"
    if re.search(r">\s*/dev/(sd|nvme|vd|hd|mmcblk)", command):
        return "deny", "redirect into a block device"
    return None


def main() -> None:
    data = json.load(sys.stdin)
    command = (data.get("tool_input") or {}).get("command") or ""
    verdict = check(command, data.get("cwd"))
    if verdict is None:
        return
    decision, reason = verdict
    sys.stdout.write(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": decision,
                    "permissionDecisionReason": f"guard-bash: {reason}",
                }
            }
        )
    )


if __name__ == "__main__":
    main()
