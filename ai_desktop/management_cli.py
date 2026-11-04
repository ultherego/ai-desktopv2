"""Local model settings and named roles. This module never starts an AI client."""
import argparse
from contextlib import contextmanager
from dataclasses import replace
import json
import sys

from . import agent_profiles as profiles, execution, model_catalog
from .terminal_style import paint

MODEL_LABELS = {"default": "Auto (domyślny klienta)", "best": "Najlepszy dostępny", "fable": "Fable",
                "opus": "Opus", "sonnet": "Sonnet", "haiku": "Haiku", "opusplan": "Tryb planowania: Opus, wykonanie: Sonnet",
                "custom": "Inny identyfikator (wpisz)"}


def model_choices(provider):
    """(values, labels, note). Claude: the client's aliases. Codex: Auto, the client's own catalog, free text."""
    if provider == "claude":
        return profiles.MODELS, tuple(MODEL_LABELS[key] for key in profiles.MODELS), None
    print("Pobieram listę modeli z klienta Codex…", flush=True)
    try:
        entries = model_catalog.fetch()
        note = None
    except model_catalog.CatalogUnavailable:
        entries, note = (), "Lista modeli z klienta jest niedostępna; wybierz Auto albo wpisz identyfikator."
    values = ("default", *(ident for ident, _ in entries), "custom")
    labels = (MODEL_LABELS["default"], *(label + " (" + ident + ")" for ident, label in entries), MODEL_LABELS["custom"])
    return values, labels, note
EFFORT_LABELS = {"default": "domyślny klienta", "low": "niski", "medium": "średni", "high": "wysoki",
                 "xhigh": "bardzo wysoki", "max": "maksymalny"}
SERVICE_LABELS = {"claude": "Claude", "codex": "Codex"}


def _profile_flags(parser, *, create=False):
    parser.add_argument("--provider", choices=("claude", "codex"),
                        help="Wybierz usługę dla wszystkich agentów")
    parser.add_argument("--model", type=execution.model_argument, metavar="MODEL")
    parser.add_argument("--effort", choices=profiles.EFFORTS, help="Poziom rozumowania")
    parser.add_argument("--instructions", help="Własne instrukcje roli, do 4096 B")
    parser.add_argument("--access", choices=profiles.ACCESSES, help="read-only blokuje zmiany plików")
    skill = parser.add_mutually_exclusive_group()
    skill.add_argument("--skill", action="append", help="Skill dołączony do roli; można podać do czterech")
    skill.add_argument("--no-skills", action="store_true", help="Usuń przypisane Skills")


def build_parser(*, prog="ai-desktop claude", provider_neutral=False):
    description = ("Lokalne ustawienia agentów, modeli, rozumowania i Skills; bez logowania i wysyłania danych"
                   if provider_neutral else
                   "Lokalne ustawienia Claude i profile agentów; bez logowania i wysyłania danych")
    parser = argparse.ArgumentParser(prog=prog, description=description)
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("show", help="Bieżący agent, model i poziom rozumowania")
    commands.add_parser("models", help="Dostępne ustawienia modelu")
    commands.add_parser("menu", help="Wybór agenta, modelu i poziomu w terminalu")
    commands.add_parser("skills", help="Skills aktywnego profilu w terminalu")
    settings = commands.add_parser("set", help="Zmień ustawienia aktywnego profilu")
    if provider_neutral:
        settings.add_argument("--provider", choices=("claude", "codex"),
                              help="Wybierz usługę dla wszystkich agentów")
    settings.add_argument("--model", choices=profiles.MODELS)
    settings.add_argument("--effort", choices=profiles.EFFORTS)
    agents = commands.add_parser("agents", help="Zarządzaj profilami")
    actions = agents.add_subparsers(dest="action")
    actions.add_parser("list", help="Lista profili")
    actions.add_parser("menu", help="Zarządzaj profilami w terminalu")
    actions.add_parser("setup", help="Przygotuj profile i przypisz gotowe Skills")
    show = actions.add_parser("show", help="Ustawienia i instrukcje wybranego profilu")
    show.add_argument("name", nargs="?")
    for action in ("create", "update"):
        command = actions.add_parser(action, help="Utwórz profil" if action == "create" else "Zmień profil")
        command.add_argument("name")
        _profile_flags(command, create=action == "create")
    for action in ("use", "remove"):
        command = actions.add_parser(action, help="Wybierz profil" if action == "use" else "Usuń nieaktywny profil")
        command.add_argument("name")
    copy = actions.add_parser("copy", help="Skopiuj profil pod nową nazwą")
    copy.add_argument("source")
    copy.add_argument("name")
    return parser


def _summary(config):
    config = config.with_provider(config.selected_provider)
    selected = config.profiles[config.active]
    print("%s %s · %s · %s %s · %s %s" % (
        paint("Agent:", "dim"), paint(config.active, "bold", "cyan"), selected.provider,
        paint("model:", "dim"), MODEL_LABELS.get(selected.model, selected.model),
        paint("rozumowanie:", "dim"), EFFORT_LABELS[selected.effort]))
    read_only = selected.access == "read-only"
    print("%s %s · %s %s" % (
        paint("Dostęp:", "dim"),
        paint("tylko odczyt", "green") if read_only else paint("zmiany po zatwierdzeniu", "yellow"),
        paint("Skills:", "dim"), ", ".join(selected.skills) or "brak"))


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
        print("Lista pochodzi z klienta Codex; dowolny inny identyfikator podasz przez --model ID." + (" " + note if note else ""))
    print("Rozumowanie: " + ", ".join(profiles.efforts_for(provider)) + ".")
    if provider == "claude":
        print("Dostępność modelu i poziomu zależy od konta Claude i klienta. Brak automatycznej zmiany modelu.")
    else:
        print("Model domyślny wybiera klient Codex. Dostępność i poziom zależą od konta i klienta.")


def _legacy_menu(store, reader, *, provider_neutral=False):
    from .question_input import read_question
    def read(prompt):
        if reader is input and sys.stdin.isatty() and sys.stdout.isatty():
            return read_question(prompt=prompt, max_bytes=128).strip()
        return reader(prompt).strip()

    config = store.load().config
    _summary(config)
    print("1. Wybierz agenta\n2. Wybierz model\n3. Poziom rozumowania\nEnter kończy")
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
            print("Identyfikator modelu Codex:")
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
                shown.append("Następna strona")
            previous = len(shown) if page else None
            if previous is not None:
                shown.append("Poprzednia strona")
            shown.append("Wróć")
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
            print("Ustawienia zmieniły się równolegle. Odświeżono menu.")

    def service(self, before):
        value = self.pick("Usługa · dla wszystkich agentów · bez logowania", ("claude", "codex"),
                          labels=("Claude", "ChatGPT / Codex"), current=before.config.selected_provider)
        if value is not None:
            selected = before.config.with_provider(value)
            if selected != before.config:
                self.store.mutate(lambda config: selected, expected=before)

    def setup(self):
        before = self.store.load()
        after = profiles.prepared(before.config.with_provider(before.config.selected_provider))
        if after != before.config:
            self.store.mutate(lambda value: after, expected=before)

    def names(self, before, *, current=None, title="Wybierz agenta", values=None):
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
            print("Brak zainstalowanych Skills.")
            return selected
        draft = selected
        while True:
            action = self.select("Skills · " + (", ".join(draft.skills) or "brak przypisanych"),
                                 ("Dodaj Skill", "Usuń przypisanie", "Podgląd Skill", "Zapisz", "Wróć"))
            if action is None or action == 4:
                return selected
            if action == 3:
                profiles.instructions_for(draft)
                return draft
            if action == 0:
                if len(draft.skills) >= 4:
                    print("Profil może mieć najwyżej cztery Skills.")
                    continue
                name = self.pick("Dodaj Skill", tuple(name for name in names if name not in draft.skills))
                if name is not None:
                    candidate = replace(draft, skills=draft.skills + (name,))
                    profiles.instructions_for(candidate)
                    draft = candidate
            elif action == 1:
                name = self.pick("Usuń przypisanie Skill", draft.skills)
                if name is not None:
                    draft = replace(draft, skills=tuple(value for value in draft.skills if value != name))
            elif action == 2:
                name = self.pick("Podgląd Skill", names)
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
                value = self.text("Identyfikator modelu Codex: ", 96)
                value = value.strip() if value is not None else None
            if value is not None:
                return replace(selected, model=value)
        elif field == "effort":
            values = profiles.efforts_for(selected.provider)
            value = self.pick("Poziom rozumowania", values,
                              labels=tuple(EFFORT_LABELS[key] for key in values), current=selected.effort)
            if value is not None:
                return replace(selected, effort=value)
        elif field == "access":
            value = self.pick("Dostęp", profiles.ACCESSES,
                              labels=("Tylko odczyt", "Zmiany po zatwierdzeniu"), current=selected.access)
            if value is not None:
                return replace(selected, access=value)
        elif field == "skills":
            return self.edit_skills(selected)
        elif field == "instructions":
            action = self.select("Instrukcje roli", ("Wpisz nowe instrukcje", "Podgląd", "Wyczyść", "Wróć"))
            if action == 0:
                value = self.text("Instrukcje (puste = anuluj): ", profiles.MAX_INSTRUCTIONS)
                if value is not None:
                    return replace(selected, instructions=value)
            elif action == 1:
                print(selected.instructions or "Brak własnych instrukcji.")
            elif action == 2:
                return replace(selected, instructions="")
        return selected

    def fields(self, selected):
        return ("Usługa: " + SERVICE_LABELS[selected.provider], "Model: " + MODEL_LABELS.get(selected.model, selected.model),
                "Rozumowanie: " + EFFORT_LABELS[selected.effort],
                "Dostęp: " + ("tylko odczyt" if selected.access == "read-only" else "zmiany po zatwierdzeniu"),
                "Instrukcje: " + ("własne" if selected.instructions else "brak"),
                "Skills: " + (", ".join(selected.skills) or "brak przypisanych"))

    def draft(self, selected):
        fields = ("model", "effort", "access", "instructions", "skills")
        while True:
            action = self.select("Nowy profil · " + SERVICE_LABELS[selected.provider],
                                 self.fields(selected)[1:] + ("Utwórz profil", "Anuluj"))
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
                print("Profil został usunięty. Odświeżono menu agentów.")
                return
            action = self.select("Profil: " + name, self.fields(selected) + ("Wróć",), default=focus)
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
            action = self.select("Agenci · " + SERVICE_LABELS[before.config.selected_provider] +
                                 " · aktywny: " + before.config.active,
                                 ("Wybierz agenta", "Utwórz profil", "Edytuj profil", "Kopiuj profil",
                                  "Usuń profil", "Przygotuj profile i Skills", "Wróć"), default=focus)
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
                    name = self.text("Nazwa nowego profilu (puste = anuluj): ", 48)
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
                    name = self.names(before, title="Edytuj profil")
                    if name is not None:
                        self.edit(name)
                elif action == 3:
                    source = self.names(before, title="Kopiuj profil")
                    if source is None:
                        continue
                    name = self.text("Nazwa kopii (puste = anuluj): ", 48)
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
                    name = self.names(before, values=removable, title="Usuń nieaktywny profil")
                    if name is not None and self.select("Usunięcie profilu", ("Usuń: " + name, "Anuluj"), default=1) == 0:
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
            action = self.select("Ustawienia · agent: " + config.active,
                                 ("Wybierz agenta",) + self.fields(selected) +
                                 ("Zarządzaj agentami", "Przygotuj profile i Skills", "Zakończ"), default=focus)
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
                print("Ustawienia: ai-desktop settings · profile: ai-desktop agents")
            return 0
        if args.command == "set":
            updates = {name: getattr(args, name) for name in ("model", "effort") if getattr(args, name) is not None}
            provider = getattr(args, "provider", None)
            if not updates and provider is None:
                parser.error("set wymaga --provider, --model lub --effort" if provider_neutral
                             else "set wymaga --model lub --effort")
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
            print("Profile i Skills są gotowe.")
            return 0
        if action == "list":
            config = store.load().config
            config = config.with_provider(config.selected_provider)
            for name, selected in config.entries:
                print("%s %s · %s · %s · %s" % (
                    "*" if name == config.active else " ", name, selected.provider,
                    selected.model, "odczyt" if selected.access == "read-only" else "zmiany po zatwierdzeniu"))
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
            print("Usunięto profil: " + name)
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
            print("Skopiowano profil: " + name)
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
        print(("Utworzono" if action == "create" else "Zmieniono") + " profil: " + name)
        return 0
    except profiles.ProfileError as exc:
        hints = {"missing": "Nie ma takiego profilu.", "exists": "Profil już istnieje.",
                 "name": "Nazwa profilu: małe litery, cyfry i łączniki, do 48 znaków.",
                 "instructions": "Instrukcje zawierają niedozwolone znaki albo przekraczają limit rozmiaru.",
                 "profile": "Niepoprawne ustawienia profilu lub model nie pasuje do wybranego klienta.",
                 "remove": "Nie można usunąć profilu domyślnego lub aktywnego. Najpierw wybierz inny profil.",
                 "changed": "Ustawienia zmieniły się równolegle. Uruchom polecenie ponownie.",
                 "skill": "Nie można wczytać przypisanego Skill. Sprawdź ai-desktop skills validate.",
                 "selection": "Niepoprawny wybór.", "provider": "To polecenie dotyczy profilu Claude."}
        print(hints.get(str(exc), "Niepoprawna lub niedostępna konfiguracja agentów. Sprawdź ~/.config/ai-desktop/agents.json (0600)."))
        return 2
    except (OSError, UnicodeError, EOFError, KeyboardInterrupt):
        print("Nie ukończono bieżącej operacji. Sprawdź zapisane ustawienia.")
        return 2
    except ValueError:
        print("Nie udało się odczytać wyboru. Uruchom menu w interaktywnym terminalu.")
        return 2
