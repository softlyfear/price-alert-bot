"""PreToolUse guard for Bash: block system-wiping commands, ask before destructive ones.

`sudo` and `rm` stay allowed in general; only the catastrophic forms are denied
(system and home roots, the project root and its ancestors, `.claude`), and a short
list of data-destroying operations asks the user first — but only when it targets the
project itself: agents rehearse such operations in scratch clones under /tmp, and a
prompt there guards nothing. Plain `VAR=...` assignments and `cd` are followed
across the command's segments; an unresolvable target is treated as the project.
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
ENV_REF = re.compile(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))")
DECLARE = {"export", "declare", "local", "readonly"}
# Plain `mktemp [-d]` always creates under $TMPDIR, never inside the project.
MKTEMP_ASSIGN = re.compile(
    r"(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=\"?\$\(mktemp(?:\s+-[dqu]+)*\)\"?"
)


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


def _expand(word: str, env: dict[str, str]) -> str | None:
    """Expand `~` and `$VAR`; None for an unknown variable or a substitution."""
    if "$(" in word or "`" in word:
        return None
    try:
        word = ENV_REF.sub(lambda m: env[m.group(1) or m.group(2)], word)
    except KeyError:
        return None
    return os.path.expanduser(word)


def _resolve(word: str, base: str | None, env: dict[str, str]) -> str | None:
    """Absolute normalized path of `word` relative to `base`; None when unknown."""
    path = _expand(word, env)
    if path is None or (base is None and not os.path.isabs(path)):
        return None
    return os.path.normpath(os.path.join(base or "/", path))


def _in_project(path: str | None, project: str) -> bool:
    """True when `path` is the project, inside it, an ancestor of it, or unknown."""
    if path is None:
        return True
    return (
        path == project
        or path.startswith(project + "/")
        or project.startswith(path.rstrip("/") + "/")
    )


def _git_target(args: list[str], here: str | None, env: dict[str, str]) -> str | None:
    """Repository git works on: cwd adjusted by `-C`, `--git-dir`, `--work-tree`."""
    target, i = here, 0
    while i < len(args) and args[i].startswith("-"):
        arg = args[i]
        if arg in ("-C", "-c") and i + 1 < len(args):
            if arg == "-C":
                target = _resolve(args[i + 1], target, env)
            i += 2
            continue
        if arg.startswith(("--git-dir=", "--work-tree=")):
            return _resolve(arg.split("=", 1)[1], target, env)
        i += 1
    return target


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
    env = dict(os.environ)
    here: str | None = cwd
    for raw in SEGMENT_SPLIT.split(command):
        mktemp = MKTEMP_ASSIGN.fullmatch(raw.strip())
        if mktemp:
            env[mktemp.group(1)] = os.path.join(env.get("TMPDIR", "/tmp"), "mktemp")
            continue
        try:
            raw_words = shlex.split(raw.strip(), posix=True)
        except ValueError:
            raw_words = raw.split()
        declared = raw_words[:1] and raw_words[0] in DECLARE
        assigned = raw_words[1:] if declared else raw_words
        if assigned and all(ASSIGNMENT.fullmatch(w) for w in assigned):
            for w in assigned:
                name, value = w.split("=", 1)
                expanded = _expand(value, env)
                if expanded is None:
                    env.pop(name, None)
                else:
                    env[name] = expanded
            continue
        words = _tokens(raw.strip())
        if not words:
            continue
        prog, args = words[0].rsplit("/", 1)[-1], words[1:]
        flags = [a for a in args if a.startswith("-")]
        targets = [a for a in args if not a.startswith("-")]

        if prog == "cd":
            dest = targets[0] if targets else "~"
            here = None if dest == "-" else _resolve(dest, here, env)
            continue
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
                if _is_recursive(flags) and _wipes_project(t, here or cwd, project):
                    return "deny", f"recursive rm of the project or .claude {t}"
                if (
                    _is_recursive(flags)
                    and re.search(r"(^|/)\.git/?$", t)
                    and _in_project(_resolve(t, here, env), project)
                ):
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
            if not _in_project(_git_target(args, here, env), project):
                continue
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
