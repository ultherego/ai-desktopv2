"""Local model settings and named roles. This module never starts an AI client."""
import argparse
from contextlib import contextmanager
from dataclasses import replace
import json
import sys

from . import agent_profiles as profiles, execution, model_catalog
from .terminal_style import paint

MODEL_LABELS = {"default": "Auto (client default)", "best": "Best available", "fable": "Fable",
                "opus": "Opus", "sonnet": "Sonnet", "haiku": "Haiku", "opusplan": "Planning: Opus, execution: Sonnet",
                "custom": "Other ID (type it)"}


def model_choices(provider):
    """(values, labels, note). Claude: the client's aliases. Codex: Auto, the client's own catalog, free text."""
    if provider == "claude":
        return profiles.MODELS, tuple(MODEL_LABELS[key] for key in profiles.MODELS), None
    if provider == "local":
        return ("default", "custom"), ("Current server model", MODEL_LABELS["custom"]), None
    print("Getting the model list from the Codex client…", flush=True)
    try:
        entries = model_catalog.fetch()
        note = None
    except model_catalog.CatalogUnavailable:
        entries, note = (), "The client's model list is unavailable; choose Auto or type an ID."
    values = ("default", *(ident for ident, _ in entries), "custom")
    labels = (MODEL_LABELS["default"], *(label + " (" + ident + ")" for ident, label in entries), MODEL_LABELS["custom"])
    return values, labels, note
EFFORT_LABELS = {"default": "client default", "low": "low", "medium": "medium", "high": "high",
                 "xhigh": "very high", "max": "maximum"}
SERVICE_LABELS = {name: execution.provider_name(name) for name in execution.PROVIDERS}


def _profile_flags(parser, *, create=False):
    parser.add_argument("--provider", choices=execution.PROVIDERS,
                        help="Choose the service for all agents")
    parser.add_argument("--model", type=execution.model_argument, metavar="MODEL")
    parser.add_argument("--effort", choices=profiles.EFFORTS, help="Reasoning level")
    parser.add_argument("--instructions", help="Your own role instructions, up to 4096 B")
    parser.add_argument("--access", choices=profiles.ACCESSES, help="read-only blocks file changes")
    skill = parser.add_mutually_exclusive_group()
    skill.add_argument("--skill", action="append", help="Skill attached to the role; may be given more than once")
    skill.add_argument("--no-skills", action="store_true", help="Remove the assigned Skills")


def build_parser(*, prog="ai-desktop claude", provider_neutral=False):
    description = ("Local settings for agents, models, reasoning and Skills; no login and no data sent"
                   if provider_neutral else
                   "Local Claude settings and agent profiles; no login and no data sent")
    parser = argparse.ArgumentParser(prog=prog, description=description)
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("show", help="Current agent, model and reasoning level")
    commands.add_parser("models", help="Available model settings")
    commands.add_parser("menu", help="Choose the agent, model and level in the terminal")
    commands.add_parser("skills", help="Skills of the active profile in the terminal")
    settings = commands.add_parser("set", help="Change the settings of the active profile")
    if provider_neutral:
        settings.add_argument("--provider", choices=execution.PROVIDERS,
                              help="Choose the service for all agents")
    settings.add_argument("--model", type=execution.model_argument, metavar="MODEL")
    settings.add_argument("--effort", choices=profiles.EFFORTS)
    agents = commands.add_parser("agents", help="Manage profiles")
    actions = agents.add_subparsers(dest="action")
    actions.add_parser("list", help="List profiles")
    actions.add_parser("menu", help="Manage profiles in the terminal")
    actions.add_parser("setup", help="Prepare profiles and assign the ready-made Skills")
    show = actions.add_parser("show", help="Settings and instructions of the chosen profile")
    show.add_argument("name", nargs="?")
    for action in ("create", "update"):
        command = actions.add_parser(action, help="Create profile" if action == "create" else "Change profile")
        command.add_argument("name")
        _profile_flags(command, create=action == "create")
    for action in ("use", "remove"):
        command = actions.add_parser(action, help="Choose profile" if action == "use" else "Remove an inactive profile")
        command.add_argument("name")
    copy = actions.add_parser("copy", help="Copy a profile under a new name")
    copy.add_argument("source")
    copy.add_argument("name")
    return parser


def _summary(config):
    config = config.with_provider(config.selected_provider)
    selected = config.profiles[config.active]
    print("%s %s · %s · %s %s · %s %s" % (
        paint("Agent:", "dim"), paint(config.active, "bold", "cyan"), selected.provider,
        paint("model:", "dim"), MODEL_LABELS.get(selected.model, selected.model),
        paint("reasoning:", "dim"), EFFORT_LABELS[selected.effort]))
    read_only = selected.access == "read-only"
    print("%s %s · %s %s" % (
        paint("Access:", "dim"),
        paint("read-only", "green") if read_only else paint("changes after approval", "yellow"),
        paint("Skills:", "dim"), ", ".join(selected.skills) or "none"))


def _show(config, name):
    config = config.with_provider(config.selected_provider)
    selected = config.profiles.get(name)
    if selected is None:
        raise profiles.ProfileError("missing")
    print(json.dumps({"name": name, "active": name == config.active, **selected.data()}, ensure_ascii=False, indent=2))


def _models(provider="claude"):
    values, labels, note = model_choices(provider)
    for key, label in zip(values, labels):
        if key != "custom":
            print("%-16s %s" % (key, label))
    if provider == "codex":
        print("This list comes from the Codex client; give any other ID with --model ID." + (" " + note if note else ""))
    print("Reasoning: " + ", ".join(profiles.efforts_for(provider)) + ".")
    if provider == "claude":
        print("Model and level availability depends on your Claude account and the client. The model never changes automatically.")
    elif provider == "local":
        print("Use default for the current server model or set the alias configured in llama-server.")
    else:
        print("The Codex client picks the default model. Availability and level depend on your account and the client.")


def _legacy_menu(store, reader, *, provider_neutral=False):
    from .question_input import read_question
    def read(prompt):
        if reader is input and sys.stdin.isatty() and sys.stdout.isatty():
            return read_question(prompt=prompt, max_bytes=128).strip()
        return reader(prompt).strip()

    config = store.load().config
    _summary(config)
    print("1. Choose agent\n2. Choose model\n3. Reasoning level\nEnter exits")
    chosen = read("> ")
    if not chosen:
        return 0
    before = store.load()
    config = before.config
    if chosen == "1":
        names = tuple(config.profiles)
        for number, name in enumerate(names, 1):
            print("%d. %s" % (number, name))
        number = read("> ")
        if not number:
            return 0
        if not number.isascii() or not number.isdigit() or not 1 <= int(number) <= len(names):
            raise profiles.ProfileError("selection")
        after = store.mutate(lambda value: value.with_provider(config.selected_provider).select(names[int(number) - 1]),
                             expected=before)
    elif chosen in ("2", "3"):
        provider = config.profiles[config.active].provider
        if provider != "claude" and not provider_neutral:
            raise profiles.ProfileError("provider")
        if chosen == "2":
            values, shown, note = model_choices(provider)
            if note:
                print(note)
        else:
            values = profiles.efforts_for(provider)
            shown = tuple(EFFORT_LABELS[key] for key in values)
        for number, label in enumerate(shown, 1):
            print("%d. %s" % (number, label))
        number = read("> ")
        if not number:
            return 0
        if not number.isascii() or not number.isdigit() or not 1 <= int(number) <= len(values):
            raise profiles.ProfileError("selection")
        field = "model" if chosen == "2" else "effort"
        picked = values[int(number) - 1]
        if picked == "custom":
            print("Codex model ID:")
            picked = read("> ").strip()
        selected = replace(config.profiles[config.active], **{field: picked})
        after = store.mutate(lambda value: value.put(config.active, selected), expected=before)
    else:
        raise profiles.ProfileError("selection")
    _summary(after.config)
    return 0


class _SettingsMenu:
    """Local edits are committed against the snapshot displayed to the owner."""
    def __init__(self, store, reader=input, chooser=None):
        from .terminal_menu import choose
        self.store, self.reader, self.choose = store, reader, chooser or choose

    def select(self, title, options, *, default=0):
        answer = self.choose(title, tuple(options), default=default)
        if answer is not None and (type(answer) is not int or not 0 <= answer < len(options)):
            raise profiles.ProfileError("selection")
        return answer

    def pick(self, title, values, *, labels=None, current=None):
        """Page bounded terminal frames; profile limits can exceed one frame."""
        values = tuple(values)
        if not values:
            return None
        labels = tuple(labels or values)
        if len(labels) != len(values):
            raise profiles.ProfileError("selection")
        page = values.index(current) // 8 if current in values else 0
        while True:
            start = page * 8
            shown = list(labels[start:start + 8])
            count = len(shown)
            following = len(shown) if start + count < len(values) else None
            if following is not None:
                shown.append("Next page")
            previous = len(shown) if page else None
            if previous is not None:
                shown.append("Previous page")
            shown.append("Back")
            selected = values.index(current) - start if current in values[start:start + 8] else 0
            answer = self.select(title, shown, default=selected)
            if answer is None or answer == len(shown) - 1:
                return None
            if answer == following:
                page += 1
            elif answer == previous:
                page -= 1
            elif answer < count:
                return values[start + answer]

    def text(self, prompt, maximum):
        from .question_input import FOLLOWUP_PROMPT, read_question
        try:
            # The terminal reader only accepts its own fixed prompts, so the label is printed first.
            if self.reader is input:
                print(prompt.rstrip(": "))
                value = read_question(prompt=FOLLOWUP_PROMPT, max_bytes=maximum)
            else:
                value = self.reader(prompt)
        except (EOFError, KeyboardInterrupt):
            return None
        if type(value) is not str:
            raise profiles.ProfileError("selection")
        return value if value.strip() else None

    def update(self, before, name, selected):
        profiles.instructions_for(selected)
        current = before.config.with_provider(before.config.selected_provider)
        if current.profiles[name] != selected:
            self.store.mutate(lambda value: value.with_provider(current.selected_provider).put(name, selected),
                              expected=before)

    @contextmanager
    def refresh_on_change(self):
        """Discard a stale edit, then let the owning menu load a fresh frame."""
        try:
            yield
        except profiles.ProfileError as exc:
            if str(exc) != "changed":
                raise
            print("The settings were changed elsewhere at the same time. The menu was refreshed.")

    def service(self, before):
        value = self.pick("Service · for all agents · no login", execution.PROVIDERS,
                          labels=("Claude", "ChatGPT / Codex", "Local llama.cpp"), current=before.config.selected_provider)
        if value is not None:
            selected = before.config.with_provider(value)
            if selected != before.config:
                self.store.mutate(lambda config: selected, expected=before)

    def setup(self):
        before = self.store.load()
        after = profiles.prepared(before.config.with_provider(before.config.selected_provider))
        if after != before.config:
            self.store.mutate(lambda value: after, expected=before)

    def names(self, before, *, current=None, title="Choose agent", values=None):
        values = tuple(before.config.profiles) if values is None else tuple(values)
        labels = tuple(name + " · " + SERVICE_LABELS[before.config.selected_provider] for name in values)
        return self.pick(title, values, labels=labels, current=current)

    def available_skills(self):
        from . import skills
        if not skills.ROOT.is_dir() or skills.ROOT.is_symlink():
            raise profiles.ProfileError("skill")
        result = []
        for name in skills.names():
            profiles.instructions_for(profiles.Profile(skills=(name,)))
            result.append(name)
        return tuple(result)

    def edit_skills(self, selected):
        names = self.available_skills()
        if not names:
            print("No Skills are installed.")
            return selected
        draft = selected
        while True:
            action = self.select("Skills · " + (", ".join(draft.skills) or "none assigned"),
                                 ("Add Skill", "Unassign", "Preview Skill", "Save", "Back"))
            if action is None or action == 4:
                return selected
            if action == 3:
                profiles.instructions_for(draft)
                return draft
            if action == 0:
                name = self.pick("Add Skill", tuple(name for name in names if name not in draft.skills))
                if name is not None:
                    candidate = replace(draft, skills=draft.skills + (name,))
                    profiles.instructions_for(candidate)
                    draft = candidate
            elif action == 1:
                name = self.pick("Unassign Skill", draft.skills)
                if name is not None:
                    draft = replace(draft, skills=tuple(value for value in draft.skills if value != name))
            elif action == 2:
                name = self.pick("Preview Skill", names)
                if name is not None:
                    # This same bounded text has passed the execution control-character checks.
                    print(profiles.instructions_for(profiles.Profile(skills=(name,))))

    def change(self, selected, field):
        if field == "model":
            values, labels, note = model_choices(selected.provider)
            if note:
                print(note)
            value = self.pick("Model · " + selected.provider, values, labels=labels,
                              current=selected.model if selected.model in values else None)
            if value == "custom":
                value = self.text(SERVICE_LABELS[selected.provider] + " model ID: ", 96)
                value = value.strip() if value is not None else None
            if value is not None:
                return replace(selected, model=value)
        elif field == "effort":
            values = profiles.efforts_for(selected.provider)
            value = self.pick("Reasoning level", values,
                              labels=tuple(EFFORT_LABELS[key] for key in values), current=selected.effort)
            if value is not None:
                return replace(selected, effort=value)
        elif field == "access":
            value = self.pick("Access", profiles.ACCESSES,
                              labels=("Read-only", "Changes after approval"), current=selected.access)
            if value is not None:
                return replace(selected, access=value)
        elif field == "skills":
            return self.edit_skills(selected)
        elif field == "instructions":
            action = self.select("Role instructions", ("Type new instructions", "Preview", "Clear", "Back"))
            if action == 0:
                value = self.text("Instructions (empty = cancel): ", profiles.MAX_INSTRUCTIONS)
                if value is not None:
                    return replace(selected, instructions=value)
            elif action == 1:
                print(selected.instructions or "No custom instructions.")
            elif action == 2:
                return replace(selected, instructions="")
        return selected

    def fields(self, selected):
        return ("Service: " + SERVICE_LABELS[selected.provider], "Model: " + MODEL_LABELS.get(selected.model, selected.model),
                "Reasoning: " + EFFORT_LABELS[selected.effort],
                "Access: " + ("read-only" if selected.access == "read-only" else "changes after approval"),
                "Instructions: " + ("custom" if selected.instructions else "none"),
                "Skills: " + (", ".join(selected.skills) or "none assigned"))

    def draft(self, selected):
        fields = ("model", "effort", "access", "instructions", "skills")
        while True:
            action = self.select("New profile · " + SERVICE_LABELS[selected.provider],
                                 self.fields(selected)[1:] + ("Create profile", "Cancel"))
            if action is None or action == 6:
                return None
            if action == 5:
                profiles.instructions_for(selected)
                return selected
            selected = self.change(selected, fields[action])

    def edit(self, name):
        fields = ("provider", "model", "effort", "access", "instructions", "skills")
        focus = 0
        while True:
            before = self.store.load()
            selected = before.config.with_provider(before.config.selected_provider).profiles.get(name)
            if selected is None:
                print("This profile was removed. The agents menu was refreshed.")
                return
            action = self.select("Profile: " + name, self.fields(selected) + ("Back",), default=focus)
            if action is None or action == 6:
                return
            focus = action
            with self.refresh_on_change():
                if action == 0:
                    self.service(before)
                else:
                    self.update(before, name, self.change(selected, fields[action]))

    def agents(self):
        focus = 0
        while True:
            before = self.store.load()
            action = self.select("Agents · " + SERVICE_LABELS[before.config.selected_provider] +
                                 " · active: " + before.config.active,
                                 ("Choose agent", "Create profile", "Edit profile", "Copy profile",
                                  "Remove profile", "Prepare profiles and Skills", "Back"), default=focus)
            if action is None or action == 6:
                return
            focus = action
            with self.refresh_on_change():
                if action == 0:
                    name = self.names(before, current=before.config.active)
                    if name is not None:
                        self.store.mutate(lambda value: value.with_provider(before.config.selected_provider).select(name),
                                          expected=before)
                elif action == 1:
                    name = self.text("New profile name (empty = cancel): ", 48)
                    if name is None:
                        continue
                    name = profiles.slug(name.strip())
                    if name in before.config.profiles:
                        raise profiles.ProfileError("exists")
                    selected = self.draft(profiles.Profile(provider=before.config.selected_provider))
                    if selected is not None:
                        self.store.mutate(lambda value: value.with_provider(before.config.selected_provider).put(name, selected),
                                          expected=before)
                elif action == 2:
                    name = self.names(before, title="Edit profile")
                    if name is not None:
                        self.edit(name)
                elif action == 3:
                    source = self.names(before, title="Copy profile")
                    if source is None:
                        continue
                    name = self.text("Name of the copy (empty = cancel): ", 48)
                    if name is None:
                        continue
                    name = profiles.slug(name.strip())
                    if name in before.config.profiles:
                        raise profiles.ProfileError("exists")
                    selected = before.config.with_provider(before.config.selected_provider).profiles[source]
                    profiles.instructions_for(selected)
                    self.store.mutate(lambda value: value.with_provider(before.config.selected_provider).copy(source, name),
                                      expected=before)
                elif action == 4:
                    removable = tuple(name for name in before.config.profiles if name not in ("default", before.config.active))
                    name = self.names(before, values=removable, title="Remove an inactive profile")
                    if name is not None and self.select("Remove profile", ("Remove: " + name, "Cancel"), default=1) == 0:
                        self.store.mutate(lambda value: value.remove(name), expected=before)
                elif action == 5:
                    self.setup()

    def run(self):
        fields = ("provider", "model", "effort", "access", "instructions", "skills")
        focus = 0
        while True:
            before = self.store.load()
            config = before.config
            selected = config.profiles[config.active]
            action = self.select("Settings · agent: " + config.active,
                                 ("Choose agent",) + self.fields(selected) +
                                 ("Manage agents", "Prepare profiles and Skills", "Exit"), default=focus)
            if action is None or action == 9:
                _summary(self.store.load().config)
                return 0
            focus = action
            with self.refresh_on_change():
                if action == 0:
                    name = self.names(before, current=config.active)
                    if name is not None:
                        self.store.mutate(lambda value: value.with_provider(config.selected_provider).select(name),
                                          expected=before)
                elif action == 1:
                    self.service(before)
                elif action < 7:
                    self.update(before, config.active, self.change(selected, fields[action - 1]))
                elif action == 7:
                    self.agents()
                elif action == 8:
                    self.setup()


def _menu(store, reader, chooser=None, *, provider_neutral=False):
    if reader is not input and chooser is None:
        return _legacy_menu(store, reader, provider_neutral=provider_neutral)
    return _SettingsMenu(store, reader, chooser).run()


def main(argv=None, *, reader=input, store=None, chooser=None,
         prog="ai-desktop claude", provider_neutral=False):
    parser = build_parser(prog=prog, provider_neutral=provider_neutral)
    args = parser.parse_args(argv)
    try:
        store = store or profiles.Store()
        if args.command == "menu":
            return _menu(store, reader, chooser, provider_neutral=provider_neutral)
        if args.command == "skills":
            ui = _SettingsMenu(store, reader, chooser)
            before = store.load()
            name = before.config.active
            ui.update(before, name, ui.edit_skills(before.config.profiles[name]))
            _summary(store.load().config)
            return 0
        if args.command == "models":
            if provider_neutral:
                config = store.load().config
                provider = config.profiles[config.active].provider
            else:
                provider = "claude"
            _models(provider)
            return 0
        if args.command in (None, "show"):
            if args.command is None and (chooser is not None or (reader is input and sys.stdin.isatty() and sys.stdout.isatty())):
                return _menu(store, reader, chooser, provider_neutral=provider_neutral)
            _summary(store.load().config)
            if args.command is None:
                print("Settings: ai-desktop settings · profiles: ai-desktop agents")
            return 0
        if args.command == "set":
            updates = {name: getattr(args, name) for name in ("model", "effort") if getattr(args, name) is not None}
            provider = getattr(args, "provider", None)
            if not updates and provider is None:
                parser.error("set requires --provider, --model or --effort" if provider_neutral
                             else "set requires --model or --effort")
            def operation(config):
                if provider_neutral:
                    config = config.with_provider(provider or config.selected_provider)
                selected = config.profiles[config.active]
                if selected.provider != "claude" and not provider_neutral:
                    raise profiles.ProfileError("provider")
                return config.put(config.active, replace(selected, **updates))
            _summary(store.mutate(operation).config)
            return 0
        action = args.action or ("menu" if chooser is not None or (reader is input and sys.stdin.isatty() and sys.stdout.isatty()) else "list")
        if action == "menu":
            _SettingsMenu(store, reader, chooser).agents()
            _summary(store.load().config)
            return 0
        if action == "setup":
            _SettingsMenu(store, reader, chooser).setup()
            print("Profiles and Skills are ready.")
            return 0
        if action == "list":
            config = store.load().config
            config = config.with_provider(config.selected_provider)
            for name, selected in config.entries:
                print("%s %s · %s · %s · %s" % (
                    "*" if name == config.active else " ", name, selected.provider,
                    selected.model, "read-only" if selected.access == "read-only" else "changes after approval"))
            return 0
        if action == "show":
            config = store.load().config
            _show(config, args.name or config.active)
            return 0
        name = profiles.slug(args.name)
        if action == "use":
            _summary(store.mutate(lambda config: config.with_provider(config.selected_provider).select(name)).config)
            return 0
        if action == "remove":
            store.mutate(lambda config: config.remove(name))
            print("Removed profile: " + name)
            return 0
        if action == "copy":
            before = store.load()
            source = profiles.slug(args.source)
            if source not in before.config.profiles:
                raise profiles.ProfileError("missing")
            if name in before.config.profiles:
                raise profiles.ProfileError("exists")
            selected = before.config.with_provider(before.config.selected_provider).profiles[source]
            profiles.instructions_for(selected)
            store.mutate(lambda value: value.with_provider(before.config.selected_provider).copy(source, name),
                         expected=before)
            print("Copied profile: " + name)
            return 0
        updates = {field: getattr(args, field) for field in ("model", "effort", "instructions", "access")
                   if getattr(args, field) is not None}
        if args.skill is not None or args.no_skills:
            updates["skills"] = tuple(args.skill or ())
        def operation(config):
            exists = name in config.profiles
            if (action == "create" and exists) or (action == "update" and not exists):
                raise profiles.ProfileError("exists" if exists else "missing")
            config = config.with_provider(args.provider or config.selected_provider)
            selected = config.profiles[name] if exists else profiles.Profile(provider=config.selected_provider)
            selected = replace(selected, **updates)
            profiles.instructions_for(selected)
            return config.put(name, selected)
        store.mutate(operation)
        print(("Created" if action == "create" else "Changed") + " profile: " + name)
        return 0
    except profiles.ProfileError as exc:
        hints = {"missing": "There is no such profile.", "exists": "This profile already exists.",
                 "name": "Profile name: lowercase letters, digits and hyphens, up to 48 characters.",
                 "instructions": "The instructions contain characters that are not allowed or exceed the size limit.",
                 "profile": "Invalid profile settings, or the model does not match the chosen client.",
                 "remove": "The default or active profile cannot be removed. Choose another profile first.",
                 "changed": "The settings were changed elsewhere at the same time. Run the command again.",
                 "skill": "Could not load an assigned Skill. Check ai-desktop skills validate.",
                 "selection": "Invalid choice.", "provider": "This command applies to a Claude profile."}
        print(hints.get(str(exc), "The agent configuration is invalid or unavailable. Check ~/.config/ai-desktop/agents.json (0600)."))
        return 2
    except (OSError, UnicodeError, EOFError, KeyboardInterrupt):
        print("The current operation did not finish. Check the saved settings.")
        return 2
    except ValueError:
        print("Could not read your choice. Run the menu in an interactive terminal.")
        return 2
