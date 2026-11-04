"""Pinned native notification envelopes and inactive remote-control metadata."""
import unittest

from ai_desktop import analysis_contract as contract, codex_protocol as codex

SECRET = "SYNTHETIC-PRIVATE-METADATA"
KNOWN_WARNING = "Codex's Linux sandbox uses bubblewrap and needs access to create user namespaces."
UNSTABLE_WARNING = ("Under-development features enabled: skip_host_skill_discovery. Under-development features are incomplete "
                    "and may behave unpredictably.")


def application():
    return contract.application_input(None, "Pytanie.", context=[]).encode()


def record(value):
    return contract.canonical(value).encode()


def notification(method, params, timestamp=123):
    return {"method": method, "params": params, "emittedAtMs": timestamp}


def inactive(**changes):
    return {"status": "disabled", "serverName": SECRET, "installationId": SECRET,
            "environmentId": None, **changes}


def sandbox_warning(**changes):
    return {"summary": KNOWN_WARNING, "details": None, **changes}


def initialized(*, limits=None):
    protocol = codex.Protocol(application(), limits=limits)
    protocol.start()
    protocol.feed(record({"id": 0, "result": {}}))
    return protocol


def thread_started(protocol):
    protocol.feed(record({"id": 1, "result": {
        "thread": {"id": "thr_a", "ephemeral": True}, "model": "synthetic-model",
        "modelProvider": codex.PROVIDER, "approvalPolicy": "never", "cwd": "/work",
        "sandbox": {"type": "readOnly", "networkAccess": False}, "instructionSources": []}}))


def completed(*, timestamped=True):
    protocol = initialized()
    def feed(method, params, when):
        value = notification(method, params, when)
        if not timestamped:
            del value["emittedAtMs"]
        return protocol.feed(record(value))
    feed("remoteControl/status/changed", inactive(serverName="", installationId=""), 2**63 - 1)
    feed("configWarning", sandbox_warning(), 0)
    feed("thread/started", {"thread": {"id": "thr_a", "ephemeral": True}}, 0)
    thread_started(protocol)
    feed("warning", {"message": codex.CODE_MODE_WARNING, "threadId": "thr_a"}, 0)      # the real pinned order
    turn = {"id": "turn_a", "status": "inProgress", "items": [], "error": None}
    feed("turn/started", {"threadId": "thr_a", "turn": turn}, 1)
    protocol.feed(record({"id": 2, "result": {"turn": turn}}))
    item = {"id": "item_a", "type": "agentMessage", "text": "Odpowiedź.", "phase": "final_answer"}
    feed("item/started", {"threadId": "thr_a", "turnId": "turn_a", "item": item, "startedAtMs": 123}, 0)
    feed("item/agentMessage/delta", {"threadId": "thr_a", "turnId": "turn_a", "itemId": "item_a",
                                    "delta": "Odpowiedź."}, 5)
    feed("item/completed", {"threadId": "thr_a", "turnId": "turn_a", "item": item, "completedAtMs": 123}, 0)
    feed("thread/status/changed", {"threadId": "thr_a", "status": {"type": "idle"}}, 2**63 - 1)
    feed("turn/completed", {"threadId": "thr_a", "turn": {
        "id": "turn_a", "status": "completed", "items": [], "error": None}}, 1)
    return protocol


class CodexNotificationTests(unittest.TestCase):
    def refused(self, protocol, value, reason="result"):
        with self.assertRaises(contract.AnalysisDenied) as caught:
            protocol.feed(record(value))
        self.assertEqual(caught.exception.reason, reason)
        self.assertNotIn(SECRET, str(caught.exception))
        return caught.exception

    def test_realistic_timestamped_stream_and_legacy_stream_have_identical_answer(self):
        for timestamped in (False, True):
            with self.subTest(timestamped=timestamped):
                protocol = completed(timestamped=timestamped)
                result = protocol.finish()
                self.assertEqual(result, "Odpowiedź.")
                self.assertEqual(result.meta["models"][0]["model"], "synthetic-model")
                self.assertTrue(protocol.remote_status_seen)
                self.assertTrue(protocol.config_warning_seen)
                self.assertNotIn(SECRET, contract.canonical(result.meta))

    def test_disabled_metadata_is_accepted_after_initialize_before_or_during_turn(self):
        for during_turn in (False, True):
            protocol = initialized()
            if during_turn:
                thread_started(protocol)
            before = (protocol.state, protocol.thread_id, protocol.turn_id, protocol.answer, protocol.usage.copy())
            self.assertEqual(protocol.feed(record(notification("remoteControl/status/changed", inactive()))), [])
            self.assertEqual((protocol.state, protocol.thread_id, protocol.turn_id, protocol.answer, protocol.usage), before)

    def test_repeated_inactive_metadata_is_refused_without_granting_remote_control(self):
        protocol = initialized()
        message = notification("remoteControl/status/changed", inactive())
        self.assertEqual(protocol.feed(record(message)), [])
        self.refused(protocol, message)

    def test_metadata_before_initialize_response_and_after_completed_turn_is_refused(self):
        for state in ("new", "initialize", "done"):
            protocol = codex.Protocol(application())
            if state == "initialize":
                protocol.start()
            elif state == "done":
                protocol = completed()
            with self.subTest(state=state):
                self.refused(protocol, notification("remoteControl/status/changed", inactive()))

    def test_active_or_associated_remote_control_is_always_refused(self):
        for changes in ({"status": "connecting"}, {"status": "connected"}, {"status": "errored"},
                        {"environmentId": "environment-a"}, {"environmentId": ""}):
            with self.subTest(changes=changes):
                self.refused(initialized(), notification("remoteControl/status/changed", inactive(**changes)),
                             "authorization")

    def test_unknown_states_fields_or_invalid_private_metadata_are_refused(self):
        cases = [{"status": "future-state"}, {"status": True}, {"environmentId": 0},
                 {"newField": SECRET}, {"serverName": None}, {"installationId": []},
                 {"serverName": "x" * 4097}, {"installationId": "x\n"},
                 {"serverName": "x\x00"}, {"installationId": "\u202e"}]
        for changes in cases:
            with self.subTest(changes=changes):
                self.refused(initialized(), notification("remoteControl/status/changed", inactive(**changes)))
        missing = inactive()
        del missing["installationId"]
        self.refused(initialized(), notification("remoteControl/status/changed", missing))
        with self.assertRaises(contract.AnalysisDenied):
            codex.startup_notification("remoteControl/status/changed", inactive(serverName="\ud800"))

    def test_outer_timestamp_is_optional_plain_integer_and_never_a_scope(self):
        for value in (None, True, False, -1, 2**63, 1.0, "123", {}, []):
            with self.subTest(timestamp=value):
                self.refused(initialized(), notification("remoteControl/status/changed", inactive(), value))
        for extra in ({"unexpected": SECRET}, {"id": "remote-request"}):
            self.refused(initialized(), {**notification("remoteControl/status/changed", inactive()), **extra})

    def test_unknown_warnings_and_requests_still_fail_closed_with_timestamp(self):
        for method in ("warning", "account/changed", "remoteControl/newCommand"):
            with self.subTest(method=method), self.assertRaises(contract.AnalysisDenied):
                initialized().feed(record(notification(method, {"message": SECRET})))
        with self.assertRaises(codex.RequestDenied):
            initialized().feed(record({"method": "shell/exec", "params": {"command": SECRET}, "id": 4}))
        self.assertFalse(codex.startup_notification("warning", {}))

    def test_ignored_metadata_remains_inside_existing_message_and_wire_budgets(self):
        protocol = initialized()
        message = record(notification("remoteControl/status/changed", inactive()))
        before = protocol.bytes, protocol.messages
        protocol.feed(message)
        self.assertEqual((protocol.bytes, protocol.messages), (before[0] + len(message) + 1, before[1] + 1))
        protocol = initialized(limits=codex.Limits(messages=1))
        self.refused(protocol, notification("remoteControl/status/changed", inactive()), "output-limit")

    def test_only_exact_known_nested_userns_warning_is_accepted_once(self):
        for optional_nulls in ({}, {"path": None}, {"range": None}, {"path": None, "range": None}):
            protocol = initialized()
            before = (protocol.state, protocol.thread_id, protocol.turn_id, protocol.answer, protocol.usage.copy())
            message = notification("configWarning", sandbox_warning(**optional_nulls))
            self.assertEqual(protocol.feed(record(message)), [])
            self.assertEqual((protocol.state, protocol.thread_id, protocol.turn_id, protocol.answer, protocol.usage), before)
            self.assertEqual(codex.startup_notification("configWarning", sandbox_warning()), "nested-userns-warning")
            self.refused(protocol, message)

    def test_other_warning_prose_or_any_nonnull_details_and_location_is_refused(self):
        for changes in ({"summary": SECRET}, {"summary": KNOWN_WARNING + " "},
                        {"summary": KNOWN_WARNING.lower()}, {"details": ""}, {"details": SECRET},
                        {"path": SECRET}, {"path": ""}, {"range": {}}, {"range": []}, {"range": 0}):
            with self.subTest(changes=changes):
                self.refused(initialized(), notification("configWarning", sandbox_warning(**changes)), "authorization")
        for params in ({"summary": KNOWN_WARNING}, sandbox_warning(extra=SECRET),
                       sandbox_warning(summary=None), sandbox_warning(summary=True)):
            self.refused(initialized(), notification("configWarning", params))

    def test_known_warning_cannot_precede_initialize_or_follow_completion(self):
        for stage in ("new", "initialize", "done"):
            protocol = codex.Protocol(application())
            if stage == "initialize":
                protocol.start()
            elif stage == "done":
                protocol = completed()
            self.refused(protocol, notification("configWarning", sandbox_warning()))

    def test_warning_and_remote_metadata_have_separate_one_notice_bounds(self):
        for order in (("remote", "warning"), ("warning", "remote")):
            protocol = initialized()
            for kind in order:
                method, params = (("remoteControl/status/changed", inactive()) if kind == "remote"
                                  else ("configWarning", sandbox_warning()))
                self.assertEqual(protocol.feed(record(notification(method, params))), [])
            self.assertTrue(protocol.remote_status_seen)
            self.assertTrue(protocol.config_warning_seen)


class CodeModeNoticeTests(unittest.TestCase):
    """The REAL pinned 0.166.0 client sends this notice right after thread/started; the first native run failed on it."""

    def in_turn(self):
        protocol = initialized()
        protocol.feed(record(notification("thread/started", {"thread": {"id": "thr_a", "ephemeral": True}})))
        thread_started(protocol)
        return protocol

    def warning(self, **changes):
        return notification("warning", {"message": codex.CODE_MODE_WARNING, "threadId": "thr_a", **changes})

    def test_the_exact_notice_for_our_thread_is_inert_and_accepted_once(self):
        protocol = self.in_turn()
        self.assertEqual(protocol.feed(record(self.warning())), [])
        self.assertTrue(protocol.code_mode_warning_seen)
        with self.assertRaises(contract.AnalysisDenied):
            protocol.feed(record(self.warning()))

    def test_any_other_warning_text_extra_field_or_foreign_thread_is_still_refused(self):
        for params in ({"message": UNSTABLE_WARNING, "threadId": "thr_a"},                 # needs the config flag, not tolerance
                       {"message": codex.CODE_MODE_WARNING + " Extra.", "threadId": "thr_a"},
                       {"message": "Run `rm -rf /` now.", "threadId": "thr_a"},
                       {"message": codex.CODE_MODE_WARNING, "threadId": "thr_other"},
                       {"message": codex.CODE_MODE_WARNING, "threadId": "thr_a", "extra": SECRET},
                       {"message": codex.CODE_MODE_WARNING}, {"threadId": "thr_a"}, {"message": 5, "threadId": "thr_a"}):
            with self.subTest(params=str(params)[:60]), self.assertRaises(contract.AnalysisDenied) as raised:
                self.in_turn().feed(record(notification("warning", params)))
            self.assertNotIn(SECRET, str(raised.exception))

    def test_the_notice_is_not_accepted_before_the_thread_exists_or_after_completion(self):
        for protocol in (initialized(), codex.Protocol(application()), completed()):
            with self.subTest(state=protocol.state), self.assertRaises(contract.AnalysisDenied):
                protocol.feed(record(self.warning()))

    def test_a_complete_real_order_exchange_still_produces_the_answer(self):
        self.assertEqual(str(completed().finish()), "Odpowiedź.")

    def test_the_fixed_command_disables_code_mode_and_silences_only_the_unstable_feature_notice(self):
        command = codex.fixed_command("/opt/codex")
        values = [command[i + 1] for i, word in enumerate(command) if word == "-c"]
        for expected in ("suppress_unstable_features_warning=true", "features.code_mode=false", "features.code_mode_host=false",
                         "features.skip_host_skill_discovery=true", "features.shell_tool=false", "sandbox_mode=\"read-only\""):
            self.assertIn(expected, values)
        self.assertEqual(len(values), len(set(values)))
