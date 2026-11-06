"""Explicit, idle-only transfer of an ask conversation to a different recipient."""
from copy import deepcopy
import json

from . import assistant_flow, conversation_state, execution
from .conversation_sessions import recipient_key, recipient_label, role_key
from .analysis_contract import terminal_text
from .terminal_consent import confirm


def switch(provider, *, prepare, talk, saved, consent, reader):
    before = execution.current() or {}
    selected = prepare(provider)
    if recipient_key(before) == recipient_key(selected):
        print("This conversation already uses that provider and account.")
        return
    if role_key(selected) != role_key(before):
        raise ValueError("session-binding")

    # Use the destination's permissions for both preview and transfer. Refusal leaves the original conversation intact.
    with execution.using(selected):
        snapshot = assistant_flow.capture_access()
        snapshot.require_claude()
        allows = snapshot.policy.allows if snapshot.policy is not None else lambda path: False
        preview = deepcopy(talk)
        preview.revalidate(allows)
        history, memory = preview.history(), preview.task_memory()
        print("Destination: %s · role: %s · model: %s" %
              (recipient_label(selected), selected["profile"], selected["model"]))
        print("Context for future requests: %d recent question/answer pairs and %d task notes. "
              "These can contain prior file excerpts and command results." %
              (len(history), len(memory["entries"])))
        assistant_flow.describe_access(snapshot)
        def show_context():
            print(terminal_text(json.dumps({"conversation": history, "memory": memory,
                                            "profile_instructions": selected["instructions"]}, ensure_ascii=False, indent=2)))
        if not confirm("switch-provider", config=selected, reader=reader, preview=show_context):
            print("Cancelled. The conversation keeps its previous recipient.")
            return
        if prepare(provider) != selected:
            raise ValueError("recipient-changed")
        snapshot.require_claude()
        if saved is not None:
            saved.transfer(selected, conversation_state.export(preview))

    execution.select(selected)
    talk.revalidate(allows)
    consent.grant(snapshot)
    print("Conversation switched to %s." % recipient_label(selected))
