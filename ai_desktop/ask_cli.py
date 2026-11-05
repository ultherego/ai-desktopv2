"""A concise question flow with explicit context and no automatic host report."""
from contextlib import ExitStack, contextmanager, nullcontext
import argparse
import json
import os
import time
import sys

from .run_controls import recipient, failure_hint, wait_message
from .execution import UNSET
from . import ask_commands, conversation_memory, conversation_state
from .conversation_sessions import SessionError
from .assistant_contract import PlanError, PLAN_CHECKS
from .analysis_cli import HINTS
from .terminal_style import paint, rule
from .analysis_contract import SERVICE_MARGIN, request_seconds, terminal_text
from .analysis_contract import (AnalysisConsent, AnalysisDenied, ContextBudgetExceeded,
                                InputBudgetExceeded, MAX_CONTEXT_BYTES, MAX_PACKET,
                                MAX_MESSAGE_BYTES, validate_question)
from .analysis_service import send_once
from .analysis_session import installation_ready, managed_session
from .analysis_terminal import render_answer
from .analysis_diagnostics import web_diagnostic
from .terminal_progress import WaitingIndicator
from .session_presence import Presence
from .terminal_consent import confirm
from .question_input import (PROMPT as QUESTION_PROMPT, PromptSession, QuestionInputError, paste_token,
                             read_question)
from .file_context import FileReadDenied
from .question_consent import ConversationConsent, QuestionConsent
from . import assistant_flow, ask_edit
from .run_controls import add_arguments, configured

ANSWER_SEPARATOR = "─" * 48
MAX_AUTO_STEPS = 5
CONTINUE_PREFIX = "Continue the task after the saved change. The user's task: "
# The provider or the network failed one request and the session closed normally: the conversation can go on. Anything
# else (cleanup, identity, sandbox, consent...) still ends it, because the next request could not be trusted to be safe.
SERVICE_FAILURES = frozenset({"timeout", "network", "rate-limit", "provider-unavailable", "web-no-results", "output-limit",
                              "result", "client"})
LOCAL_REFUSALS = (ContextBudgetExceeded, InputBudgetExceeded, FileReadDenied, ask_edit.EditDenied)
PREPARATION_PHASES = frozenset({"input", "session", "access", "plan-request", "plan", "collect", "answer-request",
                                "edit-prepare", "edit-request", "edit-review", "display", "session-save", "internal"})


def build_parser():
    parser = argparse.ArgumentParser(prog="ai-desktop ask", description="Ask the selected assistant a question")
    parser.add_argument("--question", help="The question, instead of typing it in the terminal")
    parser.add_argument("--systemd", nargs="?", const="both", choices=("system", "user", "both"),
                        help="Explicitly include the state of systemd units")
    parser.add_argument("--systemd-state", choices=("enabled", "active", "inactive", "failed", "all", "installed"),
                        help="Choose which systemd states to include")
    parser.add_argument("--weather", metavar="CITY", help="Explicitly fetch the weather for this city")
    parser.add_argument("--no-context", action="store_true", help="Send only the question, with no added context")
    parser.add_argument("--v", "--verbose", "-v", "--details", dest="details", action="store_true",
                        help="Show details, full data and diagnostics")
    parser.add_argument("--preview", action="store_true", help="Offline preview, with no model and no Internet access")
    parser.add_argument("--format", choices=("auto", "plain", "markdown"), default="auto")
    add_arguments(parser, session=True)
    return parser


def request(question, args, reader, *, context=None, access=None, task=None, write_grant=None,
            web=False, check, state=None, question_consent=None, discovery=None):
    """One immutable payload; either scoped approval or standalone fresh consent."""
    if question_consent is not None and type(question_consent) is not QuestionConsent:
        raise AnalysisDenied("consent")
    if (task is not None or web is not False or discovery is not None) and question_consent is None:
        raise AnalysisDenied("consent")
    observed = {"discovery": discovery} if discovery is not None else {}
    check()
    gate = AnalysisConsent(None, question="" if task is not None else question, task=task,
                           context=context, access=access, web=web, **observed)
    selected = json.loads(gate.payload).get("execution")
    if state is not None:
        state["execution"] = selected
    if question_consent is not None:
        question_consent.claim(gate, question, context=context, access=access, task=task,
                               write_grant=write_grant, web=web, **observed)
    if args.details:
        print("=== EXACT DATA AND RULES ===")
        print(json.dumps(json.loads(gate.payload), ensure_ascii=False, indent=2))
        print("SHA256:", gate.sha256)
    installation_ready()
    if question_consent is None and not confirm("ask-transfer", reader=reader, config=selected):
        print("Cancelled. The selected data was not sent." if state and state["submitted"]
              else "Cancelled. The question was not sent.")
        raise assistant_flow.Declined()
    gate.check(None, question="" if task is not None else question, task=task,
               context=context, access=access, web=web, sha256=gate.sha256, nonce=gate.nonce, **observed)
    check()
    # Idle until authorization/session startup is complete. On Ctrl+C the
    # indicator stays alive for cleanup, then clears before any result/prompt.
    with WaitingIndicator(output_format=getattr(args, "format", "auto"), config=selected) as progress:
        with _session(question_consent, **({} if args.details else {"quiet": True})):
            check()
            if question_consent is None:
                approval = gate.authorize(None, question=question, context=context, access=access,
                                          web=web, sha256=gate.sha256, nonce=gate.nonce, **observed)
                print(wait_message(config=selected), flush=True)
            else:
                approval = question_consent.authorize(gate, question, context=context, access=access,
                                                      task=task, write_grant=write_grant, web=web, **observed)
                question_consent.check()
            if state is not None:
                state["submitted"] = True
                state["sent_at"] = time.monotonic()
                state["limit"] = request_seconds((selected or {}).get("effort")) + SERVICE_MARGIN
            mode = "web" if web else "workspace" if task is not None else "assistant" if access is not None else "question"
            progress.phase(mode)
            try:
                answer = send_once(approval)
            finally:
                progress.phase("cleanup")
    if question_consent is not None:
        question_consent.check()
    return answer


@contextmanager
def _session(consent, **kwargs):
    """Start the privileged session; the time spent typing the sudo password is the user's, not the question's."""
    with ExitStack() as stack:
        with (consent.waiting() if consent is not None else nullcontext()):
            stack.enter_context(managed_session(**kwargs))
        yield


@contextmanager
def _lenient(enabled):
    token = assistant_flow.lenient_prose.set(bool(enabled))
    try:
        yield
    finally:
        assistant_flow.lenient_prose.reset(token)


def _choose_thread():
    """Offer saved threads of this agent at the start of an interactive `ask`; any trouble means 'new conversation'."""
    from .conversation_sessions import SessionError, Store
    from .execution import current
    from . import session_cli
    from .terminal_menu import MenuInputError
    try:
        return session_cli.select_thread(Store(), current())
    except (SessionError, MenuInputError, OSError, ValueError):
        return ("new", None)


def _offer_save(talk, phase):
    """Ask once whether to keep this conversation. Nothing is written without a yes."""
    from .conversation_sessions import SessionError, Store
    from .execution import current
    from .terminal_consent import confirm
    try:
        if not confirm("save-thread"):
            return None
        phase("session-save")
        saved = Store().create("ask", "", "", current())
        saved.save_state(conversation_state.export(talk))
        print("Conversation saved locally. You can return to it the next time you run ai-desktop ask.")
        return saved
    except (SessionError, OSError, ValueError):
        print("Could not save the conversation. Nothing was saved.")
        return None


def _continuation(question):
    """The next step of one task after an approved write; it is the program's text, carrying the user's own task once."""
    task = question[len(CONTINUE_PREFIX):] if question.startswith(CONTINUE_PREFIX) else question
    room = MAX_MESSAGE_BYTES - len(CONTINUE_PREFIX.encode("utf-8"))
    return CONTINUE_PREFIX + task.encode("utf-8")[:room].decode("utf-8", "ignore")


def _explain_refusal(refusal):
    if isinstance(refusal, ContextBudgetExceeded):
        print(f"Not sent: the JSON context is over the limit of {MAX_CONTEXT_BYTES} bytes. "
              + recipient("The selected data was not sent to Claude."))
    elif isinstance(refusal, InputBudgetExceeded):
        print(f"Not sent: the whole JSON request is over the limit of {MAX_PACKET} bytes. "
              + recipient("This request was not sent to Claude."))
    elif isinstance(refusal, ask_edit.EditDenied):
        print("Nothing was saved. Check files.read and files.write in permissions.toml, and that the directory and files exist.")
    # FileReadDenied: the local collector has already explained it; session diagnostics would obscure the cause.


def _explain_denied(exc, args, state):
    # Keep ordinary failures concise; only a closed web check is visible
    # by default. Verbose mode keeps its existing reason diagnostics.
    if args.details or web_diagnostic(exc.diagnostic) is not None:
        print(str(exc))
    if exc.reason == "consent":
        print("Time ran out for this question: the model took longer than the limit allows. The question was stopped. Ask it again "
              "or use a lower level (/effort)." if getattr(exc, "expired", False)
              else "Consent expired or the access rules changed. The question was stopped.")
    elif exc.reason == "web-no-results":
        print("No confirmed search results came back. The question ended without an answer.")
    else:
        print(failure_hint(exc.reason, HINTS.get(exc.reason, "Could not get an answer. Check ai-desktop status."),
                           config=state["execution"],
                           **({"diagnostic": exc.diagnostic} if exc.diagnostic is not None else {})))
        if exc.reason == "timeout" and "sent_at" in state:
            # Say how long the last call really waited and what the limit was, so a slow model is told from a stall.
            print(paint("The last call waited %d s (limit %d s)." % (
                time.monotonic() - state["sent_at"], state["limit"]), "dim"))


def _report_plan_error(exc, args):
    check = exc.check if type(exc.check) is str and exc.check in PLAN_CHECKS else "invalid"
    print("The model returned an invalid plan (plan-" + check + "). This plan was not carried out.")
    if args.details:
        print("ASK: REFUSED stage=plan check=" + check)


def _read_message(session, followup):
    """One user message from the terminal. A multi-line or long paste (command output, a log excerpt) is accepted: it
    shows as a short marker while editing and is expanded into the message when sent. The message itself still has the
    ordinary size limit, and the user is told the exact size when a paste does not fit."""
    while True:
        # A bare LF still submits (an unbracketed paste must never smuggle a following answer into the message);
        # only a bracketed paste becomes an attachment.
        try:
            text = read_question(max_bytes=MAX_MESSAGE_BYTES, followup=followup, session=session, attach=True, lf_breaks=False)
        except QuestionInputError as refused:
            if refused.args != ("question paste",):
                raise
            print("The paste is too large: the whole message can be at most %d KiB. Shorten it or use ai-desktop material FILE."
                  % (MAX_MESSAGE_BYTES // 1024))
            followup = True
            continue
        for number, block in session.take_pastes(text):
            text = text.replace(paste_token(number, block), block.strip("\n"))
        size = len(text.encode("utf-8"))
        if size <= MAX_MESSAGE_BYTES:
            return text
        print("With the paste the message is %d B and the limit is %d B. Shorten the pasted text (for example head -n 20) and paste it again."
              % (size, MAX_MESSAGE_BYTES))
        followup = True


@contextmanager
def _turn_context(talk, snapshot):
    """Filter history and memory with the policy that authorizes this exact attempt."""
    from .execution import using_history, using_memory
    policy = snapshot.policy
    removed = talk.revalidate((lambda path: False) if policy is None else policy.allows)
    if removed:
        print("Access to a source used earlier in this conversation was revoked: the answers and model notes "
              "that depended on it were removed from the conversation memory (%d)." % removed)
    with using_history(talk.history()), using_memory(talk.task_memory()):
        yield


def _remember(talk, conversation, question, answer, ops, sources, phase, args):
    """Record a finished, approved turn; the task memory is updated from the FULL answer before any shortening."""
    notes = talk.finish(question, answer or "[empty answer]", ops, sources, actions=getattr(sources, "actions", ()))
    if getattr(args, "presence", None) is not None:
        args.presence.update(talk.number, conversation.session_id if conversation is not None else None)
    if "trimmed" in notes:
        print("The task memory reached its limit: the oldest, less important entries were left out (the memory keeps count).")
    if args.details and "no-update" in notes:
        print("Task memory: the model sent no valid update; only your message was recorded.")
    if conversation is not None:
        phase("session-save")
        from .conversation_sessions import SessionError
        try:
            conversation.save_state(conversation_state.export(talk))
        except (SessionError, ValueError, OSError):
            # A turn that cannot be stored must not end the conversation or lose the answer on the screen.
            print("Could not save this turn to disk; the conversation continues and the turn stays in memory until you exit.")


@configured
def main(argv=None, *, reader=input):
    args = build_parser().parse_args(argv)
    state = {"submitted": False, "execution": UNSET, "phase": "input"}
    def phase(value):
        state["phase"] = value if type(value) is str and value in PREPARATION_PHASES else "internal"
    conversation = None
    args.presence = None
    try:
        if os.geteuid() == 0 or os.getuid() != os.geteuid():
            raise AnalysisDenied("identity")
        if args.no_context and any((args.systemd, args.systemd_state, args.weather)):
            raise ValueError("conflicting context selection")
        if (not args.preview or args.question is None) and not (sys.stdin.isatty() and sys.stdout.isatty()):
            raise AnalysisDenied("consent")
        if args.question is None and not args.preview and reader is input:
            args.presence = Presence()
            args.presence.start()
        question = args.question
        prompt_session = PromptSession()
        prompt_session.commands = ask_commands.COMMANDS
        prompt_session.paste_bytes = MAX_MESSAGE_BYTES
        controls = ask_commands.Controls()
        if args.preview and args.session:
            print("Preview does not save or resume a conversation. Remove --session or --preview.")
            return 2
        if question is None and not args.preview and args.session is None and reader is input:
            # The saved threads are offered BEFORE the first message, so a new question never has to be typed twice.
            phase("session")
            chosen = _choose_thread()
            if chosen is None:
                print("Cancelled.")
                return 0
            if chosen[0] == "resume":
                args.session = chosen[1]
        from .conversation_sessions import load_session
        from .execution import current
        consent = ConversationConsent()
        talk = conversation_state.Conversation()
        if args.session not in (None, "new"):
            conversation = load_session(args, "ask", execution_config=current())
            talk = conversation_state.load(conversation.state())
        def control(line):
            result = controls.handle(line, conversation, talk=talk, consent=consent, reader=reader)
            if result is True and args.presence is not None:
                args.presence.update(talk.number, conversation.session_id if conversation is not None else None)
            return result
        if question is None:
            greeted = False                    # the greeting is shown once, not again after each local command
            while True:
                question = (_read_message(prompt_session, greeted) if reader is input else reader(QUESTION_PROMPT))
                greeted = True
                validate_question(question, MAX_MESSAGE_BYTES)
                local = control(question)
                if local == ask_commands.EXIT:
                    return 0
                if local:
                    continue
                if question.strip():
                    break
                print("Type a question.")
        validate_question(question, MAX_MESSAGE_BYTES)
        if not question.strip():
            print("No question was typed. Run ai-desktop ask to try again.")
            return 2
        if args.question is not None and not args.preview:
            print(question)
        if not args.preview:
            print()
        if conversation is None:
            conversation = load_session(args, "ask", execution_config=current())
            if conversation is not None:
                talk = conversation_state.load(conversation.state())
        if conversation is not None:
            print("Conversation: %s · saved locally" % conversation.session_id)
        # Interactive loop only without --question/--preview. Every turn below is a fresh question under one yes for the
        # conversation; the conversation state is local data, never authority, and is re-validated against the CURRENT
        # policy before each turn (a revoked source takes what depended on it out of the next request).
        interactive = args.question is None and not args.preview

        def leave(result):
            """End of a conversation: offer (once, with consent) to keep it as a thread to return to."""
            nonlocal conversation
            if conversation is None and interactive and reader is input and talk.turns:
                conversation = _offer_save(talk, phase)
            return result
        if args.presence is not None:
            args.presence.update(talk.number, conversation.session_id if conversation is not None else None)
        idle, code, steps = 0, 0, 0
        sources = None

        def keep_actions():
            """A turn that fails after the program acted: what it really did must not be forgotten, or the model may
            propose the same change again. Nothing is recorded for a turn that did nothing."""
            if interactive and getattr(sources, "actions", None):
                _remember(talk, conversation, question, "[The model's answer did not arrive. The actions taken are recorded.]",
                          None, sources, phase, args)
        while True:
            finished = False
            try:
                with conversation_state.recording() as sources, _lenient(interactive):
                    answer = assistant_flow.run(question, args, reader,
                        lambda *values, **kwargs: request(*values, **kwargs, state=state), phase=phase,
                        conversation_consent=consent, skills=controls.load_skill,
                        turn_context=lambda snapshot: _turn_context(talk, snapshot))
                finished = True
            except assistant_flow.Declined:
                if not interactive:
                    raise
                answer, code = None, 2             # nothing was sent and the declined turn is not recorded
            except PlanError as exc:
                if not interactive:
                    raise
                _report_plan_error(exc, args)      # a model's bad plan loses that turn, not the whole conversation
                keep_actions()
                answer, code = None, 2
            except LOCAL_REFUSALS as refusal:
                if not interactive:
                    raise
                _explain_refusal(refusal)          # a local refusal loses that turn too; the session already closed
                keep_actions()
                answer, code = None, 2
            except AnalysisDenied as denied:
                if not interactive or denied.reason not in SERVICE_FAILURES:
                    raise
                _explain_denied(denied, args, state)
                if denied.reason == "rate-limit":
                    print("Use /provider to choose another recipient and review the conversation transfer.")
                print("The conversation continues; the question was not retried. You can ask it again.")
                keep_actions()
                answer, code = None, 2
            except KeyboardInterrupt:
                if not interactive:
                    raise
                # The turn's own cleanup (session, consent, process) has already run in the flow's finally blocks;
                # a failure there would have raised instead of landing here.
                print("\nCancelled. This turn was stopped." + (" Data already sent cannot be taken back; the question will not be retried."
                                                if state["submitted"] else ""))
                keep_actions()
                answer, code = None, 2
            if finished:
                code = 0
                if type(answer) is ask_edit.CompletedTask:
                    note = "[Change task finished; result %d. See the task log for the write and the test.]" % answer.code
                    code = answer.code
                    _remember(talk, conversation, question, note, None, sources, phase, args)
                    if not interactive:
                        return code
                    if code == 0:
                        # An applied change does not end the task: the model takes the next step or says it is done. Each
                        # further write still shows its diff and asks. A declined or failed change returns to the user.
                        if steps < MAX_AUTO_STEPS:
                            steps += 1
                            question = _continuation(question)
                            state["submitted"] = False
                            continue
                        print("Reached the limit of %d steps in one question. Type \"continue\" to go on." % MAX_AUTO_STEPS)
                elif answer is not None:
                    visible, raw = conversation_memory.split_marker(answer)
                    ops = conversation_memory.parse_ops(raw) if raw is not None else None
                    phase("display")
                    print("\n" + rule() + "\n")
                    if visible.strip():
                        print(render_answer(visible, output_format=args.format))
                    print("\n" + rule() + "\n")
                    _remember(talk, conversation, question, visible, ops, sources, phase, args)
                    if not interactive:
                        if args.details:
                            print("ASK: PASS")
                            print("SESSION: STOPPED")
                        return 0
                elif not interactive:
                    return 0
            state["submitted"] = False
            steps = 0
            phase("input")
            while True:
                try:
                    question = (_read_message(prompt_session, True) if reader is input else reader(QUESTION_PROMPT))
                except EOFError:
                    idle += 1
                    if idle >= 2:
                        print()
                        return leave(code)
                    print("\nPress Ctrl-D again (or send an empty line) to end the conversation.")
                    continue
                except KeyboardInterrupt:
                    print()
                    return leave(code)
                idle = 0
                validate_question(question, MAX_MESSAGE_BYTES)
                local = control(question)
                if local == ask_commands.EXIT:
                    return leave(code)
                if local:
                    continue
                break
            if not question.strip():
                return leave(code)
            print()
    except assistant_flow.Declined:
        return 2
    except PlanError as exc:
        _report_plan_error(exc, args)
        return 2
    except assistant_flow.AccessDenied as exc:
        if getattr(exc, "reason", None) == "policy-invalid":
            print(paint("Invalid permissions.toml. Check version and the read/write/deny rules.", "bold", "red"))
            if exc.problem:
                print(paint("Problem:", "bold", "red"))
                for line in exc.problem:
                    print("  " + paint(terminal_text(line), "yellow"))
        elif getattr(exc, "reason", None) in ("network-claude-disabled", "network-codex-disabled", "network-local-disabled"):
            from .execution import provider_name
            provider = exc.reason.split("-")[1]
            name = provider_name(provider)
            print(name + " is blocked (network." + provider + "=false).")
            print("To allow sending, set " + provider + " = true in [network] of ~/.config/ai-desktop/permissions.toml.")
        else:
            print("The access policy does not allow this operation, or it has changed. Check permissions.toml.")
        return 2
    except LOCAL_REFUSALS as refusal:
        _explain_refusal(refusal)
        return 2
    except AnalysisDenied as exc:
        _explain_denied(exc, args, state)
        return 2
    except (EOFError, KeyboardInterrupt):
        print("Cancelled.")
        if state["submitted"]:
            print("Data already sent cannot be taken back; the question will not be retried automatically.")
        return 2
    except QuestionInputError:
        print(f"Could not read the question. Use one line of plain text, up to {MAX_MESSAGE_BYTES} bytes.")
        return 2
    except SessionError as exc:
        if str(exc) == "session-binding":
            print("The saved recipient or role does not match. Check ai-desktop sessions show ID, "
                  "select the saved account or local endpoint, then run ai-desktop sessions resume ID.")
        else:
            print("Could not open the saved conversation. Check ai-desktop sessions list.")
        return 2
    except (OSError, ValueError, TypeError, KeyError) as exc:
        if args.details:
            kind = "os" if isinstance(exc, OSError) else "type" if isinstance(exc, TypeError) else "key" if isinstance(exc, KeyError) else "value"
            print("ASK: REFUSED stage=" + state["phase"] + " check=prepare-" + kind)
        print("Could not finish the question (prepare-" + state["phase"] + ").")
        return 2
    finally:
        if args.presence is not None:
            args.presence.close()
        if conversation is not None:
            conversation.close()
