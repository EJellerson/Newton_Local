"""Pure observation normalization/evidence checks; this module executes nothing.

Recovery uses the existing terminal tool and its approval machinery. Only
unambiguous read-only operations are suggested/eligible for constrained recovery.
"""
from __future__ import annotations

import re
import shlex
from pathlib import PurePosixPath

KEY = re.compile(r"\b(?:hw|kern|vm|iogpu|debug|machdep|net|user|newton)(?:\.[A-Za-z_][A-Za-z0-9_]*)+\b")
LOCAL = re.compile(r"\b(?:(?:this|my|your|current|local) (?:(?:actual|current|local) ){0,2}(?:machine|mac|system|computer)|on this mac)\b", re.I)
OBSERVE = re.compile(r"\b(?:inspect|check|read|query|measure|verify|run|execute)\b", re.I)
CLAIM = re.compile(r"\b(?:I (?:just |have |also )?(?:checked|inspected|queried|measured|verified|looked at)|"
                   r"(?:your|this) (?:machine|mac|system) (?:currently )?(?:reports|shows|has))\b", re.I)
NEGATIVE = re.compile(r"\b(?:could not|cannot|can't|couldn't|not verified|unverified|unable to|did not)\b", re.I)
DEPENDENT = re.compile(r"\b(?:should|recommend|want to|raise|change|safe|based on|depending on)\b", re.I)
NUMBER = r"[-+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)*"


def prose(text):
    return re.sub(r"```.*?```", "", text, flags=re.S)


def sysctl_keys(text):
    return list(dict.fromkeys(KEY.findall(text)))


def inspection_commands(text):
    if re.search(r"\b(?:no tools|do not (?:run|execute|use tools))\b", text, re.I):
        return []
    if LOCAL.search(text) or (OBSERVE.search(text) and "sysctl" in text.lower()):
        return ["sysctl " + key for key in sysctl_keys(text)]
    return []


def read_operation(command):
    """A small evidence grammar, not a shell parser or a command executor.

    Composite shell operations cannot establish per-operation exit status. The
    model must issue simple separate calls when structured evidence is needed.
    """
    if re.search(r"[;&|<>`$\n]", command):
        return None
    try:
        argv = shlex.split(command)
    except ValueError:
        return None
    if not argv:
        return None
    name = PurePosixPath(argv[0]).name
    if argv[0] not in (name, "/usr/sbin/" + name, "/usr/bin/" + name, "/bin/" + name):
        return None
    args = argv[1:]
    if name == "sysctl":
        if args[:1] == ["-n"]:
            args = args[1:]
        if len(args) == 1 and KEY.fullmatch(args[0]):
            return {"kind": "sysctl", "subject": args[0], "command": "sysctl " + args[0]}
    fixed = {("uname", "-m"): "architecture", ("sw_vers", "-productVersion"): "os",
             ("vm_stat",): "memory", ("memory_pressure",): "memory", ("pwd",): "directory",
             ("df", "-h"): "disk", ("ps", "-ax"): "processes",
             ("lsof", "-nP", "-iTCP", "-sTCP:LISTEN"): "ports"}
    kind = fixed.get(tuple([name] + args))
    if name == "ls" and all(arg in ("-l", "-a", "-la", "-al", "-lh", "-lah") or not arg.startswith("-") for arg in args):
        kind = "directory"
    return {"kind": kind, "subject": kind, "command": shlex.join([name] + args)} if kind else None


def tool_observation(tool, args, output):
    if tool == "terminal":
        operation = read_operation(args.get("command", ""))
        return {**operation, "value": observation_value(operation, output)} if operation else None
    fields = {"read_file": ("file", "path"), "search_files": ("file", "path"),
              "web_search": ("web", "query"), "web_extract": ("web", "urls")}
    if tool in fields:
        kind, field = fields[tool]
        target = args.get(field)
        if target:
            return {"kind": kind, "subject": str(target), "target": str(target)}
    return None


def observation_value(operation, output):
    if operation and operation["kind"] in ("os", "architecture", "directory"):
        return output.strip()
    if operation and operation["kind"] == "sysctl":
        key = operation["subject"]
        match = re.search(r"(?m)^" + re.escape(key) + r":\s*(.+)$", output)
        if match:
            return match.group(1).strip()
        if re.fullmatch(r"[-+]?\d+(?:\.\d+)?\s*", output):
            return output.strip()
    return None


def question_parts(text):
    # Small request decomposition only when an inspection already exists.
    # A model supplies the actual answers; the ledger enforces dependencies.
    parts = [p.strip() for p in re.split(r"(?<=[.!?])\s+|\n+", text) if p.strip()]
    result = []
    for part in parts:
        if len(part.split()) < 5 and result:
            result[-1] += " " + part
        else:
            result.append(part)
    return result if len(result) > 1 else []


def scalar_values(sentence, keys):
    values = []
    for key in keys:
        pattern = re.escape(key) + r"[`*]*\s*(reads|reports|returns|(?:value )?is|=|:|\|)\s*[`*]*(" + NUMBER + ")"
        for match in re.finditer(pattern, sentence, re.I):
            prefix = sentence[:match.start()]
            if match.group(1) == "=" and re.search(r"\b(?:sysctl(?:\s+-\w+)*|set)\s+$", prefix, re.I):
                continue  # proposed command/assignment, not an observed value
            values.append((key, match.group(2).replace(",", "")))
    return values


def observation_claims(text, requested_subjects=()):
    """Return paragraphs claiming external observation, excluding code examples."""
    found = []
    # A colon-form sysctl output line remains a data claim even inside a code
    # fence. Shell examples (sysctl key=value) are not output observations.
    output_lines = "\n".join(line for line in text.splitlines() if re.match(r"^\s*" + KEY.pattern + r":\s*[-+]?\d", line))
    for paragraph in re.split(r"\n\s*\n", prose(text) + "\n\n" + output_lines):
        for sentence in re.split(r"(?<=[.!?])\s+", paragraph):
            keys = sysctl_keys(sentence)
            claimed = CLAIM.search(sentence)
            if not claimed and re.match(r"\s*(?:if|when|for example|for instance)\b", sentence, re.I):
                continue
            current_value = bool(scalar_values(sentence, keys))
            if not keys and len(requested_subjects) == 1 and re.search(r"\bcurrent value\s+(?:is|reads|=|:)\s*[`*]*" + NUMBER, sentence, re.I):
                keys, current_value = list(requested_subjects), True
            if NEGATIVE.search(sentence) and not CLAIM.search(sentence):
                continue
            if claimed or current_value:
                found.append((sentence, keys))
    return found


def claim_error(text, observations, requested_subjects=()):
    """Require subject-specific current receipts, and compare quoted scalar facts.

    Broad semantic assertions remain model-assisted. Unrelated tool activity
    (including writes and bookkeeping) is never an observation receipt.
    """
    by_subject = {o["subject"]: o for o in observations}
    for sentence, keys in observation_claims(text, requested_subjects):
        subjects = keys
        if not subjects:
            categories = [(r"disk|space", "disk"), (r"process", "processes"),
                          (r"port|listen", "ports"), (r"memory|RAM", "memory"),
                          (r"\b(?:macos|operating system|os version|system version)\b", "os"),
                          (r"directory|folder|workspace", "directory"), (r"architecture|chip", "architecture")]
            subjects = [kind for pattern, kind in categories if re.search(pattern, sentence, re.I)]
        # Named file/lookup claims require a matching target, not merely any
        # successful tool. Semantic entailment of retrieved text is not proven.
        if not keys and not subjects:
            words = set(re.findall(r"[a-z0-9_]{4,}", sentence.lower())) - {"just", "checked", "inspected", "queried", "looked", "your", "this", "that", "with", "from", "have"}
            if any(words & set(re.findall(r"[a-z0-9_]{4,}", o.get("target", "").lower()))
                   for o in observations if o["kind"] in ("file", "web")):
                continue
            subjects = list(requested_subjects)
        if not subjects or any(subject not in by_subject for subject in subjects):
            return "No matching successful observation receipt for: " + sentence[:400]
        values = scalar_values(sentence, keys)
        if len(requested_subjects) == 1:
            match = re.search(r"\bcurrent value\s+(?:is|reads|=|:)\s*[`*]*(" + NUMBER + ")", sentence, re.I)
            if match:
                values.append((requested_subjects[0], match.group(1).replace(",", "")))
        for key, value in values:
            if by_subject.get(key, {}).get("value") != value:
                return "Claimed value disagrees with the receipt for " + key
    return None
