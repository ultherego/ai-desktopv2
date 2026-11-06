"""One bounded read proposal, checked against the user's captured permissions.

The model chooses data it needs, not its authority. Relative selections must
remain inside the configured scope. No commands, URLs or recursive tool loop
are accepted. One approval covers this
question and its captured permissions; each payload has its own managed session.
"""
from contextlib import nullcontext
from contextvars import ContextVar
from dataclasses import dataclass
import json
import os

from .run_controls import recipient, recipient_config, wait_message
from .execution import current as current_execution
from . import ask_context, ask_discovery, ask_edit, conversation_state, file_context, permissions, ask_followup
from .assistant_contract import PlanError, parse_plan, validate_access, _constant as _no_constant, _unique as _unique_keys
from .analysis_contract import (AnalysisDenied, ContextBudgetExceeded, InputBudgetExceeded, application_input,
                                terminal_text)
from .question_consent import QuestionConsent
from .discovery_contract import MAX_SKILL_ROUNDS
from .terminal_consent import confirm
from .terminal_style import card, paint, size_label, symbol


# Set only by the interactive conversation: a planner reply that is plain prose instead of a JSON plan is shown as an
# answer. It carries no authority (an answer never reads, runs or writes anything); one-shot `ask` stays strict.
lenient_prose = ContextVar("ai_desktop_lenient_prose", default=False)


def _plan(response, access):
    try:
        return parse_plan(response, access)
    except PlanError as error:
        text = response.strip() if type(response) is str else ""
        if error.check == "json" and lenient_prose.get() and text:
            index = text.find('{"action"')
            if index >= 0:
                # A sentence followed by a plan: the plan is what counts. A broken plan, a plan inside a code fence
                # or one that breaks the strict JSON rules (duplicate keys, NaN) is an error, never an answer.
                if "```" in text:
                    raise error
                try:
                    decoder = json.JSONDecoder(object_pairs_hook=_unique_keys, parse_constant=_no_constant)
                    plan_object, _ = decoder.raw_decode(text[index:])
                except ValueError:
                    raise error from None
                return parse_plan(json.dumps(plan_object, ensure_ascii=False), access)
            if not text.startswith(("{", "[", "```json")) and '"action"' not in text:
                return parse_plan(json.dumps({"action": "answer", "text": text}, ensure_ascii=False), access)
        raise


class AccessDenied(ValueError):
    def __init__(self, reason=None, problem=()):
        self.reason = reason if reason in ("policy-invalid", "network-claude-disabled", "network-codex-disabled", "network-local-disabled") else None
        self.problem = tuple(problem) if type(problem) in (tuple, list) and all(type(line) is str for line in problem) else ()
        super().__init__("access denied")


class Declined(Exception):
    pass


@dataclass(frozen=True)
class AccessSnapshot:
    policy: object
    sources: dict
    access: dict
    provider: str = "claude"

    def recheck(self):
        if self.policy is None:
            # Creating a policy also revokes an earlier unconfigured decision.
            if permissions.load_policy(optional=True) is not None:
                raise AccessDenied()
        else:
            self.policy.recheck()

    def require_claude(self):
        self.recheck()
        if self.sources["claude"] is not True:
            reason = "network-" + self.provider + "-disabled" if self.provider in ("claude", "codex", "local") else None
            raise AccessDenied(reason)


def _usable_writes(policy, access):
    """A write scope whose directory does not exist cannot take a file (write_workspace never creates one), so the
    model must not offer it. Ids keep their policy index; the policy itself is unchanged."""
    if "write" not in access:
        return access
    roots = {"w" + str(scope.index): scope.base.rsplit("/", 1)[0] if scope.mode == "exact" else scope.base
             for scope in policy.write_scopes()}
    usable = [item for item in access["write"] if os.path.isdir(roots[item["id"]])]
    trimmed = {key: value for key, value in access.items() if key != "write"}
    return {**trimmed, "write": usable} if usable else trimmed


def capture_access():
    try:
        policy = permissions.load_policy(optional=True)
    except (OSError, ValueError) as exc:
        reason = exc.args[0] if type(exc) is ValueError and len(exc.args) == 1 else None
        raise AccessDenied(reason, getattr(exc, "problem", ())) from None
    if policy is None:
        # Without an explicit policy the single question approval sends only
        # the question. No implicit system, file, weather or web access is granted.
        sources = {"claude": True, "weather": False, "systemd": []}
        access = {"files": [], "systemd": [], "weather": False}
    else:
        sources, access = policy.sources(), _usable_writes(policy, policy.access())
    from .execution import current
    selected = current()
    if selected is not None and selected["provider"] in ("codex", "local"):
        if policy is not None or selected["provider"] == "local":
            sources = {**sources, "claude": policy is not None and policy.provider_allowed(selected["provider"])}
        # This adapter has no external WebSearch tool. Local file/systemd and
        # explicitly selected weather remain controller-owned collectors.
        access = {key: value for key, value in access.items() if key != "web"}
        sources = {key: value for key, value in sources.items() if key != "web"}
    if selected is not None and selected["access"] == "read-only":
        # A read-only profile gets neither files to edit nor commands that change the live system.
        access = {key: value for key, value in access.items() if key not in ("write", "change_commands")}
    else:
        from .project_checks import current_directory
        if current_directory(policy) is not None:
            access = {**access, "project_test": True}  # capability only; cwd stays local
    return AccessSnapshot(policy, sources, validate_access(access),
                          "claude" if selected is None else selected["provider"])


def describe_access(snapshot):
    access = snapshot.access
    print(recipient("Access: Claude ") + ("allowed" if snapshot.sources["claude"] else "blocked")
          + "; file scopes: " + str(len(access["files"])) + "; systemd: "
          + (", ".join(access["systemd"]) or "none") + "; weather: "
          + ("allowed" if access["weather"] else "none")
          + "; Internet: " + ("allowed" if access.get("web") is True else "none")
          + "; write scopes: " + str(len(access.get("write", []))) + ".")


def _legacy_selection(args, snapshot):
    """Existing explicit flags narrow access; none can bypass the TOML policy."""
    files, systemd, weather = [], [], None
    if args.no_context:
        return {"action": "read", "files": files, "systemd": systemd, "weather": weather}
    if args.systemd_state and not args.systemd:
        raise ValueError("state requires explicit scope")
    if args.systemd:
        scopes = ("system", "user") if args.systemd == "both" else (args.systemd,)
        if any(scope not in snapshot.access["systemd"] for scope in scopes):
            raise AccessDenied()
        systemd = [{"scope": scope, "view": args.systemd_state or "installed"} for scope in scopes]
    if args.weather:
        if snapshot.access["weather"] is not True:
            raise AccessDenied()
        weather = args.weather
    return {"action": "read", "files": files, "systemd": systemd, "weather": weather}


def collect(plan, snapshot, args, reader, *, question_consent=None, defer=()):
    """Only a validated plan and the original, unchanged policy reach readers."""
    plan = parse_plan(json.dumps(plan), snapshot.access)
    if plan["action"] != "read":
        raise AccessDenied()
    if type(question_consent) is not QuestionConsent:
        raise AccessDenied()
    question_consent.check()
    context = []
    if plan["files"]:
        if snapshot.policy is None:
            raise AccessDenied()
        context.append(file_context.collect(snapshot.policy, plan["files"], args, reader,
                                            question_consent=question_consent, defer=defer))
    for selection in plan["systemd"]:
        question_consent.check()
        context.extend(ask_context._systemd(selection["scope"], selection["view"], args, reader,
                                            check=question_consent.check, question_consent=question_consent))
    if plan["weather"] is not None:
        question_consent.check()
        try:
            item = ask_context._weather(plan["weather"], args, reader, check=question_consent.check,
                                        question_consent=question_consent)
        except ask_context.weather_context.WeatherError as exc:
            print(ask_context.WEATHER_HINTS[exc.reason])
            raise ValueError("context unavailable") from None
        if item is not None:
            context.append(item)
    question_consent.check()
    if args.details:
        ask_context.preview_context(context, details=True)
    return context


def _deny(policy, code, prefix, sent):
    """Final, explained local refusal: nothing from the failed step is sent, and no partial result is passed off."""
    selected = recipient_config()
    print(prefix + ": " + file_context._read_hint(ValueError(code) if code else ValueError(), policy))
    if code in ask_discovery.RECOVERABLE or code == "discovery-required":
        print("The limit of plan corrections ran out (plan_corrections=" + str(policy.limits().plan_corrections)
              + " in [files] of permissions.toml). Narrow the question or raise the limit.")
    print(recipient(sent, config=selected))
    raise file_context.FileReadDenied("input")


COMMITTED_OUTPUT = 256
_LABEL = {"read": "Command", "change": "Change", "test": "Test after change", "undo": "Undo"}
_OUTCOME = {"declined": "declined by the user, not run", "not-allowed": "not on the list, not run",
            "unavailable": "sandbox unavailable, not run", "timeout": "timed out", "ok": "ok",
            "missing": "this program is not on the system, not run"}


def _finished(label, step):
    healthy = step["status"] == "ok" and step["exit"] in (0, None)
    state = "%s%s" % (step["status"], "" if step["exit"] is None else ", code " + str(step["exit"]))
    dot = " " + symbol("dot") + " "
    return "%s %s%s%s%s%d B %s conversation" % (
        paint(symbol("ok" if healthy else "fail"), "green" if healthy else "red", "bold"),
        paint(label, "bold"), dot, paint(state, "green" if healthy else "red"), dot,
        len(step["output"].encode("utf-8")), symbol("arrow"))


def _read_line(shown, step):
    """One row per read-only command: a whole batch stays readable on one screen."""
    healthy = step["status"] == "ok" and step["exit"] in (0, None)
    state = size_label(len(step["output"].encode("utf-8"))) if healthy else "%s%s" % (
        step["status"], "" if step["exit"] is None else ", code " + str(step["exit"]))
    return "  %s %s %s %s" % (paint(symbol("ok" if healthy else "fail"), "accent" if healthy else "red", "bold"), shown,
                              paint(symbol("dot"), "muted"), paint(state, "muted" if healthy else "red"))


def _run_command(snapshot, ledger, plan, permit, args, reader):
    """Read-only commands (sandbox, no prompt: the owner listed them in `allow`) or a CHANGE with its test and undo (live
    system, strong yes, undo only on a second yes). The controller writes what really happened to the task memory and to
    the model's next request."""
    from . import command_ops
    policy, argv = snapshot.policy, plan["argv"]
    selected = current_execution()
    shown = lambda words: terminal_text(" ".join(words))

    def add(words, role, status, code=None, output="", cut=False):
        observed = ledger.add_run({"op": "run", "argv": list(words), "status": status, "exit": code,
                                   "output": output, "truncated": cut, "role": role})
        outcome = _OUTCOME.get(status, "failed")
        conversation_state.note_action("%s %s: %s%s" % (_LABEL[role], " ".join(words), outcome,
                                                       "" if code is None else ", code %d" % code))
        return observed

    def spend():
        try:
            ledger.take_command()
            return ledger.output_room()
        except ask_discovery.BudgetExhausted as exhausted:
            if exhausted.name == "discovery_bytes" and ledger.soft_stop(exhausted.name):
                return None
            _budget_stop(policy, exhausted.name)

    def committed_room():
        """The live change already ran, so its test and undo are not optional: the byte budget may shrink their output
        but never skip them. can_afford(3) reserved their command rounds before the change."""
        ledger.commands_left = max(ledger.commands_left - 1, 0)
        try:
            return ledger.output_room()
        except ask_discovery.BudgetExhausted:
            return COMMITTED_OUTPUT

    change_ran = []

    def run_step(words, role, host):
        room = spend() if role == "change" else committed_room()
        if room is None:
            return None
        path = command_ops.resolve(words[0])
        if path is None:
            add(words, role, "not-allowed")
            return None
        permit.check()
        step = command_ops.execute(path, words, output_limit=room, timeout=policy.limits().command_timeout,
                                   host=host, role=role)
        if role == "change":
            change_ran.append(True)
        # The command has already run on the live system: its result is recorded and shown before consent can stop the
        # question. A lapsed consent stops what comes next, it does not erase what happened.
        step = add(words, role, step["status"], step["exit"], step["output"], step["truncated"])
        print(_finished(_LABEL[role], step))
        if args.details:
            print(terminal_text(step["output"]))
        permit.check()
        return step

    changing = command_ops.match(policy.changes(), argv)
    if not changing:
        if "test" in plan:
            print("A test and an undo apply only to changes from the `change` list: " + shown(argv))
            spend() is None or add(argv, "read", "not-allowed")
            return
        room = spend()
        if room is None:
            return
        path = command_ops.resolve(argv[0]) if command_ops.match(policy.commands(), argv) else None
        if path is None:
            if command_ops.match(policy.commands(), argv):
                print(paint("  %s %s %s program not found" % (symbol("skip"), shown(argv), symbol("dot")), "muted"))
                add(argv, "read", "missing")
                return
            print("This command is not on the allowed list in permissions.toml and will not run: " + shown(argv))
            add(argv, "read", "not-allowed")
            return
        permit.check()
        if args.details:
            print("%s %s sandbox: read-only, no network" % (path, symbol("dot")))
        extra = {}
        if policy.share_processes():          # owner opt-in; the default call is exactly what it always was
            extra["command"] = lambda program, words: command_ops.sandbox_command(program, words, share_processes=True)
        step = command_ops.execute(path, argv, output_limit=room, timeout=policy.limits().command_timeout, **extra)
        permit.check()
        step = add(argv, "read", step["status"], step["exit"], step["output"], step["truncated"])
        print(_read_line(shown(argv), step))
        if args.details:
            print(terminal_text(step["output"]))
        return
    # A change: it needs its test (a reading command) and its undo (a change or reading command), all named up front.
    test, undo = plan.get("test"), plan.get("undo")
    ok = (test is not None and command_ops.match(policy.commands(), test)
          and not command_ops.match(policy.changes(), test)          # a "test" must never itself change the system
          and (command_ops.match(policy.changes(), undo) or command_ops.match(policy.commands(), undo)))
    if not ok or not ledger.can_afford(3):
        # without room for change + test + undo (or without a valid test/undo) nothing is changed
        if ok:
            _budget_stop(policy, "command_rounds" if ledger.commands_left < 3 else "discovery_bytes")
        print("A change needs a test command (from the `allow` list) and an undo (from the `change` or `allow` list); not run: "
              + shown(argv))
        spend() is None or add(argv, "change", "not-allowed")
        return
    permit.check()
    print()
    for text in card("SYSTEM CHANGE · live, no sandbox", [
            "change            " + shown(argv), "test after change " + shown(test), "undo              " + shown(undo)],
            tone="red"):
        print(text)
    with permit.waiting():
        agreed = confirm("run-change", reader=reader, config=selected)
    if not agreed:
        print("The change was not made.")
        spend() is None or add(argv, "change", "declined")
        return
    try:
        changed = run_step(argv, "change", True)
        if changed is None:
            return                  # nothing ran (refused or budget closed): there is nothing to test or undo
        tested = None
        if changed["status"] == "ok":
            tested = run_step(test, "test", True)
            if tested is None:
                conversation_state.note_action("Test after change %s: skipped, no budget left" % " ".join(test))
        healthy = (changed is not None and changed["status"] == "ok" and changed["exit"] == 0
                   and tested is not None and tested["status"] == "ok" and tested["exit"] == 0)
        if healthy:
            print("Test after change passed.")
            return
        print("The change or the test after it FAILED. Undo: " + shown(undo))
        with permit.waiting():
            undo_agreed = confirm("run-undo", reader=reader, config=selected)
        if undo_agreed:
            run_step(undo, "undo", True)
        else:
            conversation_state.note_action("Undo %s: declined by the user, not run" % " ".join(undo))
            print("The undo was not run. The system may still be changed.")
    except AnalysisDenied:
        if change_ran:
            # No further command runs under a lapsed consent or changed policy, and nothing more is sent. The owner decides.
            conversation_state.note_action("Change %s: stopped after it ran, further steps not run" % " ".join(argv))
            print("The change WAS MADE, but consent expired or the permissions changed. The next steps will not "
                  "run and nothing more will be sent.")
            print("Check the system. Test: " + shown(test))
            print("You can run the undo yourself: " + shown(undo))
        raise


def _budget_stop(policy, name):
    if name == "skills_bytes":
        from .skill_runtime import MAX_KNOWLEDGE_BYTES
        print("Refused: selected Skills and references exceed their separate %d B budget." % MAX_KNOWLEDGE_BYTES)
        raise file_context.FileReadDenied("input")
    limits = policy.limits() if policy is not None else permissions.Limits()
    print("Refused: the file discovery budget ran out: " + name + "=" + str(getattr(limits, name))
          + " ([files] of permissions.toml; ceiling in the code: " + str(permissions.LIMIT_SPECS[name][2])
          + "). A partial result is not sent as if it were complete.")
    print(recipient("The local result was not sent to Claude."))
    raise file_context.FileReadDenied("input")


def _observed(carried, **payload):
    """What this task collected earlier, for its last request. It fits in nearly every case; if it does not, the request
    still goes, and the owner is told what the model will not see instead of the whole answer being refused."""
    document = carried.get("document")
    if document is None:
        return {}
    try:
        application_input(None, discovery=document, **payload)
    except (ContextBudgetExceeded, InputBudgetExceeded):
        print(paint("The results of earlier steps do not fit in the request: the model will not see them at this stage.", "muted"))
        return {}
    return {"discovery": document}


def _run_once(question, args, reader, request, *, snapshot, phase=None, conversation_consent=None, skills=None):
    """One plan and answer, optionally preceded by bounded discovery. Without a conversation grant it asks every time."""
    def progress(value):
        if phase is not None:
            phase(value)
    explicit = args.no_context or args.systemd or args.systemd_state or args.weather
    if args.preview:
        describe_access(snapshot)
        for scope in snapshot.access["files"] + snapshot.access.get("write", []):
            print("  " + scope["id"] + ": " + terminal_text(json.dumps(scope["pattern"], ensure_ascii=False)))
        if explicit:
            _legacy_selection(args, snapshot)
        if args.details:
            print(json.dumps({"question": question, "access": snapshot.access}, ensure_ascii=False, indent=2))
        print("Permissions preview: no model, no directory listing and no reading of the selected data.")
        print("Preview done. Nothing was sent.")
        return None
    snapshot.require_claude()
    explicit_plan = _legacy_selection(args, snapshot) if explicit else None
    selected = current_execution()
    permit = QuestionConsent(question, snapshot)
    try:
        if args.details:
            describe_access(snapshot)
            print(json.dumps({"question": question, "access": snapshot.access, "sources": snapshot.sources},
                             ensure_ascii=False, indent=2))
        if conversation_consent is None or not conversation_consent.covers(snapshot):
            if not confirm("ask-question", reader=reader, config=selected):
                print("Cancelled. The question was not sent.")
                raise Declined()
            if conversation_consent is not None:
                conversation_consent.grant(snapshot)
        permit.approve()
        print(wait_message(config=selected), flush=True)
        if explicit:
            progress("collect")
            context = [] if args.no_context else collect(explicit_plan, snapshot, args, reader,
                                                         question_consent=permit)
            progress("answer-request")
            answer = request(question, args, reader, context=context, check=permit.check, question_consent=permit)
        else:
            ledger = ask_discovery.DiscoveryLedger(snapshot.policy, question, (selected or {}).get("instructions", ""))
            carried = {}
            loaded = set()
            if skills is not None and conversation_consent is not None:
                for name, body in conversation_consent.reusable_skills(skills):
                    try:
                        ledger.add_skill(name, body)
                    except ask_discovery.BudgetExhausted:
                        continue
                    loaded.add(name)
                if loaded:
                    pending = ledger.snapshot()
                    try:
                        application_input(None, question=question, access=snapshot.access, discovery=pending.document)
                    except (ContextBudgetExceeded, InputBudgetExceeded):
                        ledger = ask_discovery.DiscoveryLedger(snapshot.policy, question, (selected or {}).get("instructions", ""))
                        loaded.clear()
                    else:
                        carried["document"] = permit.bind_discovery(snapshot.policy, pending, reuse_loader=skills)
            progress("plan-request")
            response = request(question, args, reader, access=snapshot.access, check=permit.check,
                               question_consent=permit, **({"discovery": carried["document"]} if carried else {}))
            permit.check()
            progress("plan")
            plan = _plan(response, snapshot.access)

            def next_plan():
                """One more planner round that carries the cumulative local observations (same single approval)."""
                try:
                    pending = ledger.snapshot()
                except ValueError:
                    _budget_stop(snapshot.policy, "discovery_bytes")
                document = permit.bind_discovery(snapshot.policy, pending, resource_loader=skills)
                carried["document"] = document
                progress("plan-request")
                reply = request(question, args, reader, access=snapshot.access, discovery=document,
                                check=permit.check, question_consent=permit)
                permit.check()
                progress("plan")
                following = _plan(reply, snapshot.access)
                if following["action"] not in ("list", "find", "run", "skill", "read", "read-range", "search-content", "answer", "edit", "delete", "project-test"):
                    raise PlanError("action")
                return following

            context = None
            skill_rounds = 0
            while True:
                if snapshot.policy is None and (plan["action"] in ("list", "find", "run", "read-range", "search-content")
                                                or plan["action"] == "read" and plan["files"]):
                    raise AccessDenied()
                if plan["action"] == "skill":
                    # On demand: until the model asks, only the name and description of a Skill leave this computer.
                    skill_rounds += 1
                    if skills is None or skill_rounds > MAX_SKILL_ROUNDS:
                        raise PlanError("action")
                    previous = {step["name"]: step["text"] for step in ledger.steps if step["op"] == "skill"}
                    wanted = [name for name in plan["names"] if name not in loaded or
                              "/" in name and skills(name) != previous.get(name)]
                    if not wanted:
                        raise PlanError("action")      # nothing new: another round would only repeat the same request
                    progress("collect")
                    for name in wanted:
                        loaded.add(name)
                        text = skills(name)
                        try:
                            ledger.add_skill(name, text)
                        except ask_discovery.BudgetExhausted as exhausted:
                            _budget_stop(snapshot.policy, exhausted.name)
                        print(paint("  %s Skill %s %s %s" % (
                            symbol("skip" if text is None else "ok"), name, symbol("dot"),
                            "no such Skill" if text is None else size_label(len(text.encode("utf-8")))), "muted"))
                    plan = next_plan()
                    if conversation_consent is not None:
                        conversation_consent.remember_skills(ledger.steps[-len(wanted):])
                    continue
                if plan["action"] == "run":
                    # An operation the owner listed in permissions.toml; its bounded result goes back to the model as
                    # data in the next planner round.
                    progress("collect")
                    if "commands" in plan:
                        # One planner step, many reads: the model gets all the results in its next single round.
                        for argv in plan["commands"]:
                            if ledger.commands_left <= 0:
                                break
                            permit.renew()    # commands may take their full timeout each; the question ceiling still holds
                            _run_command(snapshot, ledger, {"action": "run", "argv": argv}, permit, args, reader)
                    else:
                        _run_command(snapshot, ledger, plan, permit, args, reader)
                    plan = next_plan()
                    continue
                if plan["action"] in ("read-range", "search-content"):
                    progress("collect")
                    try:
                        ledger.content(plan, permit)
                        permit.check()
                    except ask_discovery.Recoverable as problem:
                        if not ledger.correction(problem):
                            _deny(snapshot.policy, problem.code, "Content read refused", "The excerpt was not sent to Claude.")
                    except ask_discovery.BudgetExhausted as exhausted:
                        if not ledger.soft_stop(exhausted.name):
                            _budget_stop(snapshot.policy, exhausted.name)
                    except (OSError, ValueError, TypeError, KeyError, AttributeError, UnicodeError) as error:
                        code = error.args[0] if isinstance(error, ValueError) and len(error.args) == 1 else None
                        if (type(code) is not str or code not in ask_discovery.RECOVERABLE
                                or not ledger.correction(ask_discovery.Recoverable(
                                    code, ledger.selectors([plan["file"]]),
                                    snapshot.policy.file_size() if code == "files-size" else ledger.limit_for(code)))):
                            _deny(snapshot.policy, code if type(code) is str else None, "Content read refused",
                                  "The excerpt was not sent to Claude.")
                    plan = next_plan()
                    continue
                if plan["action"] in ("list", "find"):
                    # Discovery is an explicit, budgeted controller operation, never an open-ended tool loop.
                    progress("collect")
                    try:
                        ledger.run(plan, permit)
                        permit.check()
                    except ask_discovery.BudgetExhausted as exhausted:
                        if not ledger.soft_stop(exhausted.name):
                            _budget_stop(snapshot.policy, exhausted.name)
                    except (OSError, ValueError, TypeError, KeyError, AttributeError, UnicodeError) as error:
                        code = error.args[0] if isinstance(error, ValueError) and len(error.args) == 1 else None
                        if (type(code) is not str or code not in ask_discovery.RECOVERABLE
                                or not ledger.correction(ask_discovery.Recoverable(
                                    code, ledger.selectors(plan["files"]), ledger.limit_for(code)))):
                            _deny(snapshot.policy, code if type(code) is str else None, "Directory listing refused",
                                  "The directory listing was not sent to Claude.")
                    plan = next_plan()
                    continue
                if plan["action"] != "read" or not plan["files"]:
                    break
                try:
                    ledger.ground(plan["files"])
                    progress("collect")
                    context = collect(plan, snapshot, args, reader, question_consent=permit,
                                      defer=ask_discovery.RECOVERABLE)
                    break
                except (ask_discovery.Recoverable, file_context.FileReadDenied) as refused:
                    if type(refused) is file_context.FileReadDenied and refused.code is None:
                        raise
                    problem = refused if type(refused) is ask_discovery.Recoverable else ask_discovery.Recoverable(
                        refused.code, ledger.selectors(plan["files"]), ledger.limit_for(refused.code))
                    if not ledger.correction(problem):
                        _deny(snapshot.policy, problem.code, "File read refused",
                              "The file contents were not sent to Claude.")
                    plan = next_plan()
            if plan["action"] == "project-test":
                from . import project_checks
                result = project_checks.current(plan["recipe"], reader, policy=snapshot.policy, permit=permit)
                if result.summary:
                    conversation_state.note_sources(result.sources)
                    conversation_state.note_action(result.summary)
                return result
            if plan["action"] == "answer":
                return plan["text"]
            if plan["action"] == "web":
                # Web is a separate final request: the exact original question,
                # never local context, file contents or a write proposal.
                permit.require_web()
                progress("answer-request")
                answer = request(question, args, reader, web=True, check=permit.check,
                                 question_consent=permit)
                permit.check()
                return answer
            if plan["action"] == "delete":
                # Local only: nothing goes to the model, and the owner's own yes comes after the list of files.
                progress("edit-review")
                permit.check()
                with permit.waiting():
                    return ask_edit.delete_files(plan, snapshot, reader)
            if plan["action"] == "edit":
                progress("edit-prepare")
                prepared = ask_edit.prepare(plan, snapshot, args, reader, question_consent=permit)
                progress("edit-request")
                response = request(question, args, reader, task=prepared.source, write_grant=prepared.write_grant,
                                   check=permit.check, question_consent=permit, **_observed(carried, task=prepared.source))
                permit.check()
                # Transfer permission is finished. The subsequent test/review
                # uses the captured write guard and a separate TaskPlan TTL.
                permit.close()
                progress("edit-review")
                return ask_edit.finish(prepared, response, args, reader)
            if context is None:
                progress("collect")
                context = collect(plan, snapshot, args, reader, question_consent=permit)
            progress("answer-request")
            answer = request(question, args, reader, context=context, check=permit.check, question_consent=permit,
                             **_observed(carried, question=question, context=context))
        permit.check()
        return answer
    finally:
        permit.close()


from .skill_runtime import version_scope


@version_scope
def run(question, args, reader, request, *, phase=None, conversation_consent=None, skills=None, turn_context=None):
    """One ordinary turn, or at most two collision follow-ups.

    Each turn closes the previous QuestionConsent and opens a new one; the prompt
    is skipped only under the conversation's grant for the same permissions.
    A reply is an instruction to prepare a new plan, never a write approval.
    Provider failures, policy refusals and cleanup errors are not retried.
    """
    for turn in range(ask_followup.MAX_FOLLOWUPS + 1):
        try:
            if phase is not None:
                phase("access")
            snapshot = capture_access()
            with turn_context(snapshot) if turn_context is not None else nullcontext():
                return _run_once(question, args, reader, request, snapshot=snapshot, phase=phase,
                                 conversation_consent=conversation_consent, skills=skills)
        except ask_edit.CreateCollision as conflict:
            if turn == ask_followup.MAX_FOLLOWUPS:
                print("The conflict was not resolved after two follow-ups. Nothing was saved.")
                raise Declined() from None
            question = ask_followup.clarify(question, conflict.path, workspace=conflict.workspace, reader=reader)
            if question is None:
                print("Cancelled. The existing file stays unchanged.")
                raise Declined() from None
    raise AssertionError("unreachable collision turn")
