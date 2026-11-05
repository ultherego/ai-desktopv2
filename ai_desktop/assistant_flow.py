"""One bounded read proposal, checked against the user's captured permissions.

The model chooses data it needs, not its authority. Relative selections must
remain inside the configured scope. No commands, URLs or recursive tool loop
are accepted. One approval covers this
question and its captured permissions; each payload has its own managed session.
"""
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
        self.reason = reason if reason in ("policy-invalid", "network-claude-disabled", "network-codex-disabled") else None
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
            reason = "network-" + self.provider + "-disabled" if self.provider in ("claude", "codex") else None
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
    if selected is not None and selected["provider"] == "codex":
        if policy is not None:
            sources = {**sources, "claude": policy.provider_allowed("codex")}
        # This adapter has no external WebSearch tool. Local file/systemd and
        # explicitly selected weather remain controller-owned collectors.
        access = {key: value for key, value in access.items() if key != "web"}
        sources = {key: value for key, value in sources.items() if key != "web"}
    if selected is not None and selected["access"] == "read-only":
        # A read-only profile gets neither files to edit nor commands that change the live system.
        access = {key: value for key, value in access.items() if key not in ("write", "change_commands")}
    return AccessSnapshot(policy, sources, validate_access(access),
                          "claude" if selected is None else selected["provider"])


def describe_access(snapshot):
    access = snapshot.access
    print(recipient("Dostępy: Claude ") + ("dozwolony" if snapshot.sources["claude"] else "zablokowany")
          + "; " + str(len(access["files"])) + " zakresów plików; systemd: "
          + (", ".join(access["systemd"]) or "brak") + "; pogoda: "
          + ("dozwolona" if access["weather"] else "brak")
          + "; Internet: " + ("dozwolony" if access.get("web") is True else "brak")
          + "; zapis: " + str(len(access.get("write", []))) + " zakresów.")


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
        print("Wyczerpano limit poprawek planu (plan_corrections=" + str(policy.limits().plan_corrections)
              + " w [files] permissions.toml). Zawęź pytanie albo zwiększ limit.")
    print(recipient(sent, config=selected))
    raise file_context.FileReadDenied("input")


COMMITTED_OUTPUT = 256
_LABEL = {"read": "Polecenie", "change": "Zmiana", "test": "Test po zmianie", "undo": "Wycofanie"}
_OUTCOME = {"declined": "odrzucone przez użytkownika, nie uruchomiono", "not-allowed": "poza listą, nie uruchomiono",
            "unavailable": "piaskownica niedostępna, nie uruchomiono", "timeout": "przekroczono czas", "ok": "ok",
            "missing": "nie ma tego programu w systemie, nie uruchomiono"}


def _finished(label, step):
    healthy = step["status"] == "ok" and step["exit"] in (0, None)
    state = "%s%s" % (step["status"], "" if step["exit"] is None else ", kod " + str(step["exit"]))
    dot = " " + symbol("dot") + " "
    return "%s %s%s%s%s%d B %s rozmowa" % (
        paint(symbol("ok" if healthy else "fail"), "green" if healthy else "red", "bold"),
        paint(label, "bold"), dot, paint(state, "green" if healthy else "red"), dot,
        len(step["output"].encode("utf-8")), symbol("arrow"))


def _read_line(shown, step):
    """One row per read-only command: a whole batch stays readable on one screen."""
    healthy = step["status"] == "ok" and step["exit"] in (0, None)
    state = size_label(len(step["output"].encode("utf-8"))) if healthy else "%s%s" % (
        step["status"], "" if step["exit"] is None else ", kod " + str(step["exit"]))
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
        ledger.add_run({"op": "run", "argv": list(words), "status": status, "exit": code, "output": output,
                        "truncated": cut, "role": role})
        outcome = _OUTCOME.get(status, "niepowodzenie")
        conversation_state.note_action("%s %s: %s%s" % (_LABEL[role], " ".join(words), outcome,
                                                       "" if code is None else ", kod %d" % code))

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
        add(words, role, step["status"], step["exit"], step["output"], step["truncated"])
        print(_finished(_LABEL[role], step))
        if args.details:
            print(terminal_text(step["output"]))
        permit.check()
        return step

    changing = command_ops.match(policy.changes(), argv)
    if not changing:
        if "test" in plan:
            print("Test i wycofanie dotyczą tylko zmian z listy `change`: " + shown(argv))
            spend() is None or add(argv, "read", "not-allowed")
            return
        room = spend()
        if room is None:
            return
        path = command_ops.resolve(argv[0]) if command_ops.match(policy.commands(), argv) else None
        if path is None:
            if command_ops.match(policy.commands(), argv):
                print(paint("  %s %s %s brak tego programu" % (symbol("skip"), shown(argv), symbol("dot")), "muted"))
                add(argv, "read", "missing")
                return
            print("Polecenie poza listą dozwolonych w permissions.toml, nie zostanie uruchomione: " + shown(argv))
            add(argv, "read", "not-allowed")
            return
        permit.check()
        if args.details:
            print("%s %s piaskownica: tylko odczyt, bez sieci" % (path, symbol("dot")))
        extra = {}
        if policy.share_processes():          # owner opt-in; the default call is exactly what it always was
            extra["command"] = lambda program, words: command_ops.sandbox_command(program, words, share_processes=True)
        step = command_ops.execute(path, argv, output_limit=room, timeout=policy.limits().command_timeout, **extra)
        permit.check()
        add(argv, "read", step["status"], step["exit"], step["output"], step["truncated"])
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
        print("Zmiana wymaga polecenia testu (z listy `allow`) i wycofania (z listy `change` lub `allow`); nie wykonano: "
              + shown(argv))
        spend() is None or add(argv, "change", "not-allowed")
        return
    permit.check()
    print()
    for text in card("ZMIANA systemu · na żywo, bez piaskownicy", [
            "zmiana          " + shown(argv), "test po zmianie " + shown(test), "wycofanie       " + shown(undo)],
            tone="red"):
        print(text)
    with permit.waiting():
        agreed = confirm("run-change", reader=reader, config=selected)
    if not agreed:
        print("Nie wykonano zmiany.")
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
                conversation_state.note_action("Test po zmianie %s: pominięty, brak budżetu" % " ".join(test))
        healthy = (changed is not None and changed["status"] == "ok" and changed["exit"] == 0
                   and tested is not None and tested["status"] == "ok" and tested["exit"] == 0)
        if healthy:
            print("Test po zmianie zaliczony.")
            return
        print("Zmiana lub test po zmianie NIE powiodły się. Wycofanie: " + shown(undo))
        with permit.waiting():
            undo_agreed = confirm("run-undo", reader=reader, config=selected)
        if undo_agreed:
            run_step(undo, "undo", True)
        else:
            conversation_state.note_action("Wycofanie %s: odrzucone przez użytkownika, nie uruchomiono" % " ".join(undo))
            print("Nie wykonano wycofania. Stan systemu może być zmieniony.")
    except AnalysisDenied:
        if change_ran:
            # No further command runs under a lapsed consent or changed policy, and nothing more is sent. The owner decides.
            conversation_state.note_action("Zmiana %s: przerwano po wykonaniu, dalsze kroki nie uruchomione" % " ".join(argv))
            print("Zmiana została WYKONANA, ale zgoda wygasła lub zmieniły się uprawnienia. Kolejne kroki nie zostaną "
                  "uruchomione i nic więcej nie zostanie wysłane.")
            print("Sprawdź stan systemu. Test: " + shown(test))
            print("Wycofanie możesz uruchomić sam: " + shown(undo))
        raise


def _budget_stop(policy, name):
    limits = policy.limits()
    print("Odmowa: wyczerpano budżet odkrywania plików: " + name + "=" + str(getattr(limits, name))
          + " ([files] permissions.toml; pułap w kodzie: " + str(permissions.LIMIT_SPECS[name][2])
          + "). Nie wysyłam niepełnego wyniku jako kompletnego.")
    print(recipient("Wynik lokalny nie został wysłany do Claude."))
    raise file_context.FileReadDenied("input")


MAX_SKILL_ROUNDS = 3


def _observed(carried, **payload):
    """What this task collected earlier, for its last request. It fits in nearly every case; if it does not, the request
    still goes, and the owner is told what the model will not see instead of the whole answer being refused."""
    document = carried.get("document")
    if document is None:
        return {}
    try:
        application_input(None, discovery=document, **payload)
    except (ContextBudgetExceeded, InputBudgetExceeded):
        print(paint("Wyniki wcześniejszych kroków nie mieszczą się w żądaniu: model ich nie zobaczy w tym etapie.", "muted"))
        return {}
    return {"discovery": document}


def _run_once(question, args, reader, request, *, phase=None, conversation_consent=None, skills=None):
    """One plan and answer, optionally preceded by bounded discovery. Without a conversation grant it asks every time."""
    def progress(value):
        if phase is not None:
            phase(value)
    progress("access")
    snapshot = capture_access()
    explicit = args.no_context or args.systemd or args.systemd_state or args.weather
    if args.preview:
        describe_access(snapshot)
        for scope in snapshot.access["files"] + snapshot.access.get("write", []):
            print("  " + scope["id"] + ": " + terminal_text(json.dumps(scope["pattern"], ensure_ascii=False)))
        if explicit:
            _legacy_selection(args, snapshot)
        if args.details:
            print(json.dumps({"question": question, "access": snapshot.access}, ensure_ascii=False, indent=2))
        print("Podgląd uprawnień — bez modelu, wyliczania katalogów i odczytu wskazanych danych.")
        print("Podgląd gotowy. Niczego nie wysłano.")
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
                print("Anulowano. Pytanie nie zostało wysłane.")
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
            progress("plan-request")
            response = request(question, args, reader, access=snapshot.access, check=permit.check,
                               question_consent=permit)
            permit.check()
            progress("plan")
            plan = _plan(response, snapshot.access)
            ledger = None
            if plan["action"] in ("list", "find", "run", "skill") or (plan["action"] == "read" and plan["files"]):
                if snapshot.policy is None:
                    raise AccessDenied()
                ledger = ask_discovery.DiscoveryLedger(snapshot.policy, question,
                                                       (current_execution() or {}).get("instructions", ""))

            carried = {}

            def next_plan():
                """One more planner round that carries the cumulative local observations (same single approval)."""
                try:
                    pending = ledger.snapshot()
                except ValueError:
                    _budget_stop(snapshot.policy, "discovery_bytes")
                document = permit.bind_discovery(snapshot.policy, pending)
                carried["document"] = document
                progress("plan-request")
                reply = request(question, args, reader, access=snapshot.access, discovery=document,
                                check=permit.check, question_consent=permit)
                permit.check()
                progress("plan")
                following = _plan(reply, snapshot.access)
                if following["action"] not in ("list", "find", "run", "skill", "read", "answer", "edit", "delete"):
                    raise PlanError("action")
                return following

            context = None
            skill_rounds, loaded = 0, set()
            while ledger is not None:
                if plan["action"] == "skill":
                    # On demand: until the model asks, only the name and description of a Skill leave this computer.
                    skill_rounds += 1
                    if skills is None or skill_rounds > MAX_SKILL_ROUNDS:
                        raise PlanError("action")
                    wanted = [name for name in plan["names"] if name not in loaded]
                    if not wanted:
                        raise PlanError("action")      # nothing new: another round would only repeat the same request
                    progress("collect")
                    for name in wanted:
                        loaded.add(name)
                        text = skills(name)
                        ledger.add_skill(name, text)
                        print(paint("  %s Skill %s %s %s" % (
                            symbol("skip" if text is None else "ok"), name, symbol("dot"),
                            "nie ma takiego" if text is None else size_label(len(text.encode("utf-8")))), "muted"))
                    plan = next_plan()
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
                            _deny(snapshot.policy, code if type(code) is str else None, "Odmowa podglądu katalogów",
                                  "Podgląd katalogów nie został wysłany do Claude.")
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
                        _deny(snapshot.policy, problem.code, "Odmowa odczytu plików",
                              "Treść plików nie została wysłana do Claude.")
                    plan = next_plan()
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


def run(question, args, reader, request, *, phase=None, conversation_consent=None, skills=None):
    """One ordinary turn, or at most two collision follow-ups.

    Each turn closes the previous QuestionConsent and opens a new one; the prompt
    is skipped only under the conversation's grant for the same permissions.
    A reply is an instruction to prepare a new plan, never a write approval.
    Provider failures, policy refusals and cleanup errors are not retried.
    """
    for turn in range(ask_followup.MAX_FOLLOWUPS + 1):
        try:
            return _run_once(question, args, reader, request, conversation_consent=conversation_consent, skills=skills,
                             **({"phase": phase} if phase is not None else {}))
        except ask_edit.CreateCollision as conflict:
            if turn == ask_followup.MAX_FOLLOWUPS:
                print("Nie rozstrzygnięto kolizji po dwóch doprecyzowaniach. Niczego nie zapisano.")
                raise Declined() from None
            question = ask_followup.clarify(question, conflict.path, workspace=conflict.workspace, reader=reader)
            if question is None:
                print("Anulowano. Istniejący plik pozostaje bez zmian.")
                raise Declined() from None
    raise AssertionError("unreachable collision turn")
