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

from . import observations as obs

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
        for task in self.data["tasks"].values():
            # Additive migration: historical terminal tasks must not poison a
            # new unrelated turn, while pending work remains durable.
            task.setdefault("turn_seq", self.data.get("turn_seq", 0))
            task.setdefault("kind", "observation" if obs.read_operation(task.get("required_command") or "") else "action")
            task.setdefault("depends_on", [])
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
                "kind": "observation" if command and obs.read_operation(command) else "action",
                "turn_seq": self.data.get("turn_seq", 0), "depends_on": [],
                "minimum_receipt": minimum if minimum is not None else len(self.data["receipts"]) + 1,
                "receipt_ids": [], "reason": None}
        return task_id

    def begin(self, request):
        self.retries = 0
        self.data["turn_seq"] = self.data.get("turn_seq", 0) + 1
        text = text_content(request)
        self.data["request"] = text
        self.data["turn_receipt_start"] = len(self.data["receipts"]) + 1
        self.data["last_rejection"] = None
        self.data["bookkeeping_stalls"] = 0
        self.data.pop("final_response_source", None)
        self.data.pop("finalization_reason", None)
        if re.search(r"\b(?:cancel|stop) (?:the |all |previous |remaining )?(?:task|work|checks|requests)", text, re.I):
            for task in self.unresolved():
                task.update(status="cancelled", reason="User cancelled the work")
        elif re.search(r"(?:^|[.!?]\s+)(?:please\s+|can you\s+|could you\s+)?(?:continue|resume|retry|try again)\b", text, re.I):
            for task in self.data["tasks"].values():
                if task["status"] in ("blocked", "failed", "incomplete"):
                    task["status"] = "pending"
                    task["turn_seq"] = self.data["turn_seq"]
                    task["minimum_receipt"] = len(self.data["receipts"]) + 1
        inspections = obs.inspection_commands(text)
        for command in inspections:
            self.add("Inspect current local value: " + command.split()[-1], "user", command=command)
        if inspections and re.search(r"\b(?:run|execute|terminal)\b", text, re.I):
            for command in re.findall(r"`([^`\n]+)`", text):
                operation = obs.read_operation(command)
                if not operation or operation["command"] not in inspections:
                    self.add("Run " + command, "user", command=command)
        if not inspections and ACTION.search(text) and not re.search(r"\b(?:no tools|do not (?:run|execute|use tools))\b", text, re.I):
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
        operations = [t for t in self.current_tasks() if t.get("kind") == "observation"]
        if operations:
            for part in obs.question_parts(text):
                # Pure imperative inspection clauses are already represented.
                if ("?" not in part and obs.OBSERVE.search(part)
                        and not re.search(r"\b(?:explain|discuss|compare|should|why|what|how)\b", part, re.I)):
                    continue
                tid = self.add(part, "answer")
                task = self.data["tasks"][tid]
                task["kind"] = "answer"
                if ((obs.LOCAL.search(part) and (obs.DEPENDENT.search(part) or obs.sysctl_keys(part)))
                        or re.search(r"\bbased on (?:its|the|that|this) (?:result|value|inspection)", part, re.I)):
                    task["depends_on"] = [t["id"] for t in operations]
        self.data["status"] = "continuing" if self.unresolved() else "completed"
        self.save()

    def requested(self, calls):
        ids = []
        for call in calls:
            fn = call.get("function", {})
            rid = len(self.data["receipts"]) + 1
            self.data["receipts"].append({"id": rid, "call_id": call.get("id"),
                "tool": fn.get("name"), "arguments": fn.get("arguments", "{}"),
                "turn_seq": self.data.get("turn_seq", 0),
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
            args = receipt["arguments"]
            try:
                args = json.loads(args) if isinstance(args, str) else args
                observation = obs.tool_observation(receipt["tool"], args, payload.get("output", "") if isinstance(payload, dict) else "")
            except (ValueError, AttributeError):
                observation = None
            if observation:
                receipt["observation"] = observation
        self.save()

    def consume(self):
        for receipt in self.data["receipts"]:
            if receipt.get("result_received", receipt["executed"]) and not receipt.get("consumption_not_required"):
                receipt["result_consumed"] = True
                if receipt["succeeded"] is True and receipt["executed"]:
                    task = self.data["tasks"].get("tool-" + str(receipt["id"]))
                    if task:
                        task.update(status="completed", receipt_ids=[receipt["id"]],
                                    reason="Successful operation result consumed by model")
                elif receipt.get("failed") or not receipt["executed"]:
                    task = self.data["tasks"].get("tool-" + str(receipt["id"]))
                    if task:
                        task.update(status="failed" if receipt["executed"] else "blocked", receipt_ids=[receipt["id"]],
                                    reason="Tool failed or requires approval; no successful observation")
        self.refresh_observations()
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

    def current_tasks(self):
        return [t for t in self.data["tasks"].values() if t.get("turn_seq", self.data.get("turn_seq")) == self.data.get("turn_seq")
                or t in self.unresolved()]

    def observations(self):
        return [r["observation"] for r in self.data["receipts"]
                if r["id"] >= self.data.get("turn_receipt_start", 1) and r.get("observation")
                and r["executed"] and r["succeeded"] is True and r["result_consumed"]]

    def requested_subjects(self):
        subjects = obs.sysctl_keys(self.data.get("request", ""))
        for task in self.current_tasks():
            operation = obs.read_operation(task.get("required_command") or "")
            if operation and operation["subject"] not in subjects:
                subjects.append(operation["subject"])
        return subjects

    def refresh_observations(self):
        for task in self.unresolved():
            if not task.get("required_command"):
                continue
            for receipt in self.data["receipts"]:
                if self.valid_receipts(task, [receipt["id"]]):
                    task.update(status="completed", receipt_ids=[receipt["id"]], reason="Matching command receipt consumed",
                                turn_seq=self.data["turn_seq"])
                    break
                try:
                    args = json.loads(receipt["arguments"]) if isinstance(receipt["arguments"], str) else receipt["arguments"]
                    direct_match = receipt["tool"] == "terminal" and args.get("command", "").strip() == task["required_command"].strip()
                except (ValueError, AttributeError):
                    direct_match = False
                operation = obs.read_operation(task["required_command"])
                matched = direct_match or (operation and receipt.get("observation", {}).get("command") == operation["command"])
                if (matched and receipt["id"] >= task["minimum_receipt"]
                        and receipt["result_consumed"] and (receipt["failed"] or not receipt["executed"])):
                    task.update(status="failed" if receipt["executed"] else "blocked", receipt_ids=[receipt["id"]],
                                reason="Inspection failed or needs approval; value is unverified", turn_seq=self.data["turn_seq"])
                    break
        for task in self.current_tasks():
            deps = [self.data["tasks"][tid] for tid in task.get("depends_on", [])]
            if any(d["status"] in ("failed", "blocked", "incomplete") for d in deps):
                task.update(status="blocked", reason="Depends on an unverified inspection", answer=None, turn_seq=self.data["turn_seq"])

    def valid_receipts(self, task, receipt_ids):
        found = []
        for rid in receipt_ids:
            if not isinstance(rid, int) or not 0 < rid <= len(self.data["receipts"]):
                return []
            receipt = self.data["receipts"][rid - 1]
            if (rid < task["minimum_receipt"] or receipt["tool"] in HOUSEKEEPING
                    or not receipt["executed"] or receipt["succeeded"] is not True or not receipt["result_consumed"]):
                return []
            if task["source"] == "observation" and not receipt.get("observation"):
                return []
            if task["required_command"]:
                if receipt["tool"] != "terminal":
                    return []
                try:
                    args = json.loads(receipt["arguments"]) if isinstance(receipt["arguments"], str) else receipt["arguments"]
                    cmd = args.get("command", args.get("cmd", ""))
                except (ValueError, AttributeError):
                    return []
                operation = obs.read_operation(task["required_command"])
                if operation:
                    if receipt.get("observation", {}).get("command") != operation["command"]:
                        return []
                    found.append(rid)
                    continue
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
            if task.get("kind") == "answer":
                return {"error": "Use answers with task_id and text for answer subtasks.", **self.view()}
            if disposition == "completed" and not self.valid_receipts(task, receipt_ids):
                return {"error": "Completion requires corresponding successful, consumed tool receipts.", **self.view()}
            if disposition == "not_required" and task["source"] != "promise":
                return {"error": "An explicit requested task cannot be dismissed as not required."}
            if disposition == "blocked" and task.get("kind") == "observation" and task["status"] == "pending":
                return {"error": "Available read-only inspection has no attempt receipt; perform it before claiming it is blocked.", **self.view()}
        for tid in task_ids:
            self.data["tasks"][tid].update(status="completed" if disposition == "not_required" else disposition,
                                          reason=reason[:2000], receipt_ids=receipt_ids, turn_seq=self.data["turn_seq"])
        self.save()
        return {"success": True, **self.view()}

    def answer(self, answers):
        self.refresh_observations()
        updates, errors = [], []
        for entry in answers:
            task = self.data["tasks"].get(entry.get("task_id"))
            text = entry.get("text", "").strip()
            if task and task.get("kind") != "answer" and task["status"] == "completed":
                # Some models include a verified operation alongside answers.
                # Its receipt, not this redundant prose, already resolved it.
                continue
            if not task or task.get("kind") != "answer" or not text:
                errors.append("Each answer needs an existing answer task_id and nonempty text.")
                continue
            if promised_work(text):
                errors.append("An answer cannot contain unfinished promised work: " + task["id"])
                continue
            deps = [self.data["tasks"][tid] for tid in task.get("depends_on", [])]
            if any(d["status"] != "completed" for d in deps):
                if any(d["status"] in ("failed", "blocked", "incomplete") for d in deps):
                    continue  # deterministic unverified/dependency notice replaces this answer
                errors.append("Inspect the required local state before answering dependent question " + task["id"])
                continue
            error = obs.claim_error(text, self.observations(), self.requested_subjects())
            if error:
                errors.append(task["id"] + ": " + error)
                continue
            failed = [t for t in self.current_tasks() if t.get("kind") == "observation" and t["status"] in ("failed", "blocked")]
            if failed and obs.LOCAL.search(text) and obs.DEPENDENT.search(text) and not obs.NEGATIVE.search(text):
                errors.append("An independent answer cannot smuggle in a recommendation dependent on failed local inspection: " + task["id"])
                continue
            updates.append((task, text))
        for task, text in updates:
            task.update(status="completed", answer=text, reason="Answer supplied; observation claims and dependencies checked",
                        turn_seq=self.data["turn_seq"])
        self.save()
        result = {"success": not errors, "accepted_answer_ids": [t["id"] for t, _ in updates], **self.view()}
        if errors:
            result["error"] = "; ".join(errors)
        return result

    def prepare_candidate(self, reply):
        """Called before the native loop persists or delivers a candidate final."""
        self.refresh_observations()
        error = obs.claim_error(reply, self.observations(), self.requested_subjects())
        self.data["last_rejection"] = error
        if error:
            existing = [t for t in self.current_tasks() if t.get("kind") == "observation"]
            if not existing:
                keys = obs.sysctl_keys(reply) or obs.sysctl_keys(self.data.get("request", ""))
                for key in keys:
                    self.add("Verify current local value: " + key, "observation", command="sysctl " + key,
                             minimum=self.data.get("turn_receipt_start", 1))
                if not keys:
                    self.add("Verify the claimed external observation with a corresponding read tool", "observation",
                             minimum=self.data.get("turn_receipt_start", 1))
            self.save()
            reply = "The proposed observation has not been verified. Required evidence or a corrected answer is still needed."
        elif self.render_answers() and not any(t.get("kind") == "answer" for t in self.unresolved()):
            reply = self.render_answers()
        self._prepared_candidate = reply
        return reply

    def context(self):
        if self.current_tasks() and not self.unresolved():
            return ("All recorded work has a terminal disposition. Finish directly with the verified results and any "
                    "explicit partial-completion limitation. Do not repeat bookkeeping or successful/failed inspections.\n" + json.dumps(self.view()))
        return ("Local execution contract: the existing terminal tool executes on the configured local host. "
                "Use separate simple read-only commands for each inspection (no sudo, assignments, pipes or command lists). "
                "Do not change system settings. A command receipt resolves only its matching observation. "
                "For answer subtasks, call task_result with answers:[{task_id,text}] containing the actual useful answer "
                "to EACH independent question. Answer dependent questions only after their inspections succeed. "
                "Failed observations are unverified; preserve independent answers and leave dependent recommendations unresolved. "
                "Do not claim a benchmark ranking or system-setting interpretation was verified by a raw sysctl read. "
                "Do not call task_result with a disposition but missing task_ids/receipt_ids.\n" + json.dumps(self.view()))

    def recovery(self):
        missing = [t for t in self.unresolved() if t.get("kind") != "answer"]
        operations = [{"task_id": t["id"], "required_operation": t["description"],
                       "tool": "terminal" if t.get("required_command") else "choose the corresponding available read tool",
                       "arguments": {"command": t["required_command"]} if t.get("required_command") else None}
                      for t in missing]
        return ("ACTION RECOVERY: no valid matching receipt exists for the operations below. "
                "Call the specified available tool now; do not narrate an intention or invent its output. "
                "If an actual failure receipt already exists, do not retry it automatically. "
                "Use the existing successful receipts and correct any contradicted claim. "
                "Then finish the unanswered independent parts.\n" + json.dumps(operations) + "\n" +
                (self.data.get("last_rejection") or "") + "\n" + self.context())

    def render_answers(self):
        return "\n\n".join(t["answer"] for t in self.current_tasks() if t.get("kind") == "answer" and t.get("answer"))

    def finalizable(self):
        tasks = self.current_tasks()
        return (any(t.get("kind") == "answer" for t in tasks) and not self.unresolved()
                and all(t["status"] in ("completed", "failed", "blocked", "cancelled") for t in tasks))

    def progress_signature(self):
        # Rewording an already accepted answer or adding housekeeping receipts
        # is not progress on a remaining obligation.
        return tuple(sorted((t["id"], t["status"]) for t in self.current_tasks()))

    def view(self):
        # Accepted prose stays in durable storage for final rendering. Sending
        # it back inside every bookkeeping receipt creates recursive context
        # growth and obscures the pending task IDs and actual evidence.
        return {"tasks": [{k: v for k, v in t.items() if k != "answer"} for t in self.current_tasks()], "receipts": [
            {**{k: r[k] for k in ("id", "tool", "arguments", "executed", "succeeded", "result_consumed")},
             "observation": r.get("observation")}
            for r in self.data["receipts"] if r["tool"] not in HOUSEKEEPING][-24:]}

    def record_promises(self, reply):
        for promise in promised_work(reply):
            pending_observations = [t for t in self.unresolved() if t.get("kind") == "observation"]
            verbs = {m.group().lower() for m in ACTION.finditer(promise)}
            subjects = {obs.read_operation(t["required_command"])["subject"] for t in pending_observations}
            keys = set(obs.sysctl_keys(promise))
            bare_read = ACTION.sub("", INTENT.sub("", promise)).strip(" .!:").lower() in ("", "now")
            refers_to_known_read = (bool(pending_observations) and verbs <= {"check", "inspect", "read", "query", "verify"}
                                   and (bool(keys) and keys <= subjects or bool(obs.LOCAL.search(promise))
                                        or bool(re.search(r"\b(?:that|this|it)\b", promise, re.I)) or bare_read))
            if not refers_to_known_read:
                self.add(promise, "promise", minimum=len(self.data["receipts"]) + 1)

    def gate(self, reply, max_retries=3):
        if getattr(self, "_prepared_candidate", None) != reply:
            reply = self.prepare_candidate(reply)
        self._prepared_candidate = None
        self.record_promises(reply)
        # Deterministic fast path for explicit shell commands: a consumed
        # successful receipt for that exact command resolves its obligation.
        self.refresh_observations()
        pending = self.unresolved()
        # Never interpret arbitrary "can't"/"failed" in an explanatory answer
        # as a blocker for unrelated tasks. Actual receipts or task_result own it.
        if pending or self.data.get("last_rejection"):
            if self.retries < max_retries:
                self.retries += 1
                self.data["status"] = "continuing"
                self.save()
                return "continuing", self.recovery()
            status = "incomplete"
        elif any(t["status"] in ("blocked", "failed") for t in self.current_tasks()):
            status = "blocked"
        elif self.data["tasks"] and all(t["status"] == "cancelled" for t in self.data["tasks"].values()):
            status = "cancelled"
        else:
            status = "completed"
        answers = self.render_answers()
        if status in ("incomplete", "blocked") and answers:
            status = "partial"
        if answers:
            reply = answers
        if status != "completed":
            remaining = [t["description"] for t in self.current_tasks() if t["status"] != "completed" and t["source"] != "tool"]
            notice = ("Partial completion" if status == "partial" else status.capitalize()) + " — unverified or unfinished: " + "; ".join(remaining)
            if any(t.get("depends_on") and t["status"] != "completed" for t in self.current_tasks()):
                notice += "\nMachine-specific recommendations depending on missing observations remain unresolved."
            reply = notice + "\n\n" + reply
        self.data["status"] = status
        self.save()
        return status, reply
