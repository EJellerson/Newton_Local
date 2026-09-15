"""Small durable completion ledger, extending Hermes' existing tool/Todo loop.

Known obligations are never erased by a fresh frontend request. Tool receipts
and model-declared task resolution are separate events. No inference is used
to adjudicate a terminal transition.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import time

HOUSEKEEPING = {"todo", "memory", "skill_manage", "session_search", "task_result"}
ACTION = re.compile(r"\b(?:run|execute|inspect|check|verify|look up|search|read|write|create|open|fetch|query)\b", re.I)
INTENT = re.compile(r"\b(?:let me|i['’]ll|i will|i am going to|i'm going to|i’m going to|"
                    r"i can (?:check|verify|inspect|run)|(?:doing|checking|running|executing|working on) it now|"
                    r"(?:doing|checking|running) (?:that|this) now|next[, :] i)\b", re.I)
BLOCKED = re.compile(r"\b(?:cannot|can't|unable to|blocked|permission denied|failed)\b", re.I)


def text_content(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(v.get("text", "") for v in value if isinstance(v, dict))
    return ""


def promised_work(text):
    # Inspect sentence/clause scope, including a final promise after a long
    # answer. Quoted code/examples and conditional offers are not commitments.
    clean = re.sub(r"```.*?```|`[^`]*`|\"[^\"]*\"|“[^”]*”", "", text, flags=re.S)
    candidates = []
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", clean):
        if re.search(r"\b(?:if you|would you|you can|you could)\b", sentence, re.I):
            continue
        if INTENT.search(sentence) and (ACTION.search(sentence) or re.search(r"\bdoing\b", sentence, re.I)):
            candidates.append(sentence.strip())
    return candidates


def result_payload(content):
    text = text_content(content).strip()
    if text.startswith("<untrusted_tool_result"):
        text = text.split(">", 1)[1].rsplit("</untrusted_tool_result>", 1)[0].strip()
        # Hermes may add an explanatory line before the serialized result.
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        for match in re.finditer(r"\{", text):
            try:
                return json.JSONDecoder().raw_decode(text[match.start():])[0]
            except ValueError:
                pass
    return None


def outcome(content):
    """True/False/None: execution evidence, not an assertion of task completion."""
    data = result_payload(content)
    if isinstance(data, dict):
        if data.get("approval_pending") or data.get("status") in ("pending_approval", "running", "pending", "background"):
            return None
        if data.get("error") or data.get("success") is False or data.get("status") in ("error", "failed", "blocked"):
            return False
        for key in ("exit_code", "returncode", "exitCode"):
            if key in data and data[key] is not None:
                return data[key] == 0
        if data.get("success") is True:
            return True
        if any(k in data for k in ("output", "results", "content", "stdout", "text", "files")):
            return True
    return None


class Ledger:
    def __init__(self, path, session_id):
        self.path, self.session_id = str(path), session_id
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.path, timeout=10, check_same_thread=False)
        self.db.execute("CREATE TABLE IF NOT EXISTS execution (session_id TEXT PRIMARY KEY, state TEXT NOT NULL)")
        row = self.db.execute("SELECT state FROM execution WHERE session_id=?", (session_id,)).fetchone()
        self.data = json.loads(row[0]) if row else {"tasks": {}, "receipts": [], "status": "completed"}
        self.retries = 0

    def save(self):
        with self.lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO execution VALUES (?, ?)",
                            (self.session_id, json.dumps(self.data, sort_keys=True)))

    def close(self):
        self.db.close()

    def add(self, description, source, *, command=None, minimum=None, task_id=None):
        seed = str(self.data.get("turn_seq", 0)) + description + str(minimum)
        task_id = task_id or source + "-" + hashlib.sha256(seed.encode()).hexdigest()[:10]
        if task_id not in self.data["tasks"]:
            self.data["tasks"][task_id] = {"id": task_id, "description": description[:2000],
                "source": source, "status": "pending", "required_command": command,
                "minimum_receipt": minimum if minimum is not None else len(self.data["receipts"]) + 1,
                "receipt_ids": [], "reason": None}
        return task_id

    def begin(self, request):
        self.retries = 0
        self.data["turn_seq"] = self.data.get("turn_seq", 0) + 1
        text = text_content(request)
        if re.search(r"\b(?:cancel|stop) (?:the |all |previous |remaining )?(?:task|work|checks|requests)", text, re.I):
            for task in self.unresolved():
                task.update(status="cancelled", reason="User cancelled the work")
        elif re.search(r"\b(?:continue|resume|retry|try again)\b", text, re.I):
            for task in self.data["tasks"].values():
                if task["status"] in ("blocked", "failed", "incomplete"):
                    task["status"] = "pending"
        if ACTION.search(text) and not re.search(r"\b(?:no tools|do not (?:run|execute|use tools))\b", text, re.I):
            commands = re.findall(r"`([^`\n]+)`", text)
            commands = commands if re.search(r"\b(?:run|execute|terminal)\b", text, re.I) else []
            if commands:
                for command in commands:
                    self.add("Run " + command, "user", command=command)
            else:
                parts = re.split(r"\n\s*(?:\d+[.)]|[-*])\s+", text)
                for part in parts:
                    if ACTION.search(part):
                        self.add(part.strip(), "user")
        self.data["status"] = "continuing" if self.unresolved() else "completed"
        self.save()

    def requested(self, calls):
        ids = []
        for call in calls:
            fn = call.get("function", {})
            rid = len(self.data["receipts"]) + 1
            self.data["receipts"].append({"id": rid, "call_id": call.get("id"),
                "tool": fn.get("name"), "arguments": fn.get("arguments", "{}"),
                "requested": True, "executed": False, "succeeded": None,
                "failed": False, "result_received": False, "result_consumed": False, "result_sha256": None})
            if fn.get("name") not in HOUSEKEEPING:
                self.add("Execute " + str(fn.get("name")), "tool", minimum=rid,
                         task_id="tool-" + str(rid))
            ids.append(rid)
        if self.unresolved():
            self.data["status"] = "continuing"
        self.save()
        return ids

    def results(self, ids, messages):
        results = [m for m in messages if m.get("role") == "tool"]
        for rid in ids:
            receipt = self.data["receipts"][rid - 1]
            index = next((i for i, m in enumerate(results) if m.get("tool_call_id") == receipt["call_id"]), None)
            if index is None:
                continue
            content = text_content(results.pop(index).get("content"))
            success = outcome(content)
            payload = result_payload(content)
            denied = isinstance(payload, dict) and (payload.get("approval_pending") or
                payload.get("status") in ("pending_approval", "blocked", "cancelled"))
            receipt.update(executed=not bool(denied), result_received=True,
                           succeeded=success, failed=success is False,
                           result_sha256=hashlib.sha256(content.encode()).hexdigest(),
                           result_summary=content[:1200])
        self.save()

    def consume(self):
        for receipt in self.data["receipts"]:
            if receipt.get("result_received", receipt["executed"]):
                receipt["result_consumed"] = True
                if receipt["succeeded"] is True and receipt["executed"]:
                    task = self.data["tasks"].get("tool-" + str(receipt["id"]))
                    if task:
                        task.update(status="completed", receipt_ids=[receipt["id"]],
                                    reason="Successful operation result consumed by model")
        self.save()

    def sync_todos(self, todos):
        for todo in todos:
            tid = "todo-" + str(todo["id"])
            if tid not in self.data["tasks"]:
                self.add(todo["content"], "todo", task_id=tid, minimum=1)
            # A model marking a todo complete does not manufacture a receipt.
            # Keep the ledger task until task_result validates its disposition.
        self.save()

    def unresolved(self):
        return [t for t in self.data["tasks"].values() if t["status"] in ("pending", "in_progress", "incomplete")]

    def valid_receipts(self, task, receipt_ids):
        found = []
        for rid in receipt_ids:
            if not isinstance(rid, int) or not 0 < rid <= len(self.data["receipts"]):
                return []
            receipt = self.data["receipts"][rid - 1]
            if (rid < task["minimum_receipt"] or receipt["tool"] in HOUSEKEEPING
                    or not receipt["executed"] or receipt["succeeded"] is not True or not receipt["result_consumed"]):
                return []
            if task["required_command"]:
                if receipt["tool"] != "terminal":
                    return []
                try:
                    args = json.loads(receipt["arguments"]) if isinstance(receipt["arguments"], str) else receipt["arguments"]
                    cmd = args.get("command", args.get("cmd", ""))
                except (ValueError, AttributeError):
                    return []
                # exit 0 for an && chain proves each command succeeded; a
                # semicolon/newline list can hide an earlier failed command.
                parts = re.split(r"\s*&&\s*", cmd)
                if task["required_command"].strip() not in [p.strip() for p in parts]:
                    return []
            found.append(rid)
        return found

    def resolve(self, task_ids, disposition, receipt_ids, reason):
        if not reason or disposition not in ("completed", "blocked", "not_required"):
            return {"error": "Provide a reason and completed, blocked, or not_required disposition."}
        if not task_ids or any(tid not in self.data["tasks"] for tid in task_ids):
            return {"error": "Unknown task IDs", **self.view()}
        for tid in task_ids:
            task = self.data["tasks"][tid]
            if disposition == "completed" and not self.valid_receipts(task, receipt_ids):
                return {"error": "Completion requires corresponding successful, consumed tool receipts.", **self.view()}
            if disposition == "not_required" and task["source"] != "promise":
                return {"error": "An explicit requested task cannot be dismissed as not required."}
        for tid in task_ids:
            self.data["tasks"][tid].update(status="completed" if disposition == "not_required" else disposition,
                                          reason=reason[:2000], receipt_ids=receipt_ids)
        self.save()
        return {"success": True, **self.view()}

    def view(self):
        return {"tasks": list(self.data["tasks"].values()), "receipts": [
            {k: r[k] for k in ("id", "tool", "arguments", "executed", "succeeded", "result_consumed")}
            for r in self.data["receipts"][-24:]]}

    def gate(self, reply, max_retries=3):
        for promise in promised_work(reply):
            self.add(promise, "promise", minimum=len(self.data["receipts"]) + 1)
        # Deterministic fast path for explicit shell commands: a consumed
        # successful receipt for that exact command resolves its obligation.
        for task in self.unresolved():
            if task["required_command"]:
                for receipt in self.data["receipts"]:
                    if self.valid_receipts(task, [receipt["id"]]):
                        task.update(status="completed", receipt_ids=[receipt["id"]], reason="Exact command receipt consumed")
                        break
        pending = self.unresolved()
        if pending and BLOCKED.search(reply) and not promised_work(reply):
            for task in pending:
                task.update(status="blocked", reason=reply[:2000])
            pending = []
        if pending:
            if self.retries < max_retries:
                self.retries += 1
                self.data["status"] = "continuing"
                self.save()
                return "continuing", ("Required work remains. Execute the required tools; then use task_result "
                    "to resolve listed tasks with successful receipt IDs, or explicitly report a blocker. "
                    "Do not repeat an intention or claim completion without evidence. After disposition, "
                    "answer the user's request directly without discussing this bookkeeping.\n" + json.dumps(self.view()))
            status = "incomplete"
        elif any(t["status"] == "blocked" for t in self.data["tasks"].values()):
            status = "blocked"
        elif self.data["tasks"] and all(t["status"] == "cancelled" for t in self.data["tasks"].values()):
            status = "cancelled"
        else:
            status = "completed"
        self.data["status"] = status
        self.save()
        return status, reply
