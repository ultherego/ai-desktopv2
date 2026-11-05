"""Local controls typed at the `ask` prompt: nothing here is sent to the model and the conversation stays as it is.

Called only for what the owner typed at the prompt, never for model output. A control can change which model or
which instructions the next question uses; it can never widen permissions, which permissions.toml alone decides.
"""
from dataclasses import replace
import re

from . import agent_profiles as profiles, execution, run_controls
from .conversation_sessions import SessionError
from .terminal_menu import MenuInputError, choose

COMMANDS = (
    ("/pomoc", "lista poleceń"),
    ("/model", "zmień model"),
    ("/sila", "zmień poziom rozumowania"),
    ("/skills", "włącz lub wyłącz Skille w tej rozmowie"),
    ("/agent", "zmień agenta"),
    ("/ustawienia", "pokaż bieżące ustawienia"),
    ("/uprawnienia", "pokaż, do czego program ma dostęp"),
    ("/koniec", "zakończ rozmowę"),
)
_ALIASES = {"/help": "/pomoc", "/?": "/pomoc", "/effort": "/sila", "/settings": "/ustawienia", "/permissions": "/uprawnienia",
            "/exit": "/koniec", "/quit": "/koniec", "/q": "/koniec"}
_SHAPE = re.compile(r"/[^/\s]*\Z")           # `/etc/fstab to co?` is a question, `/model` is a command
EXIT = "exit"
PAGE = 8
_NEXT, _PREVIOUS, _APPLY = "Następna strona", "Poprzednia strona", "Zastosuj"
_AGAIN = "Następne pytanie poprosi o zgodę jeszcze raz, bo zmieniły się ustawienia. Rozmowa trwa dalej."


def _pick(title, options, default=0):
    """An index into `options`, or None when cancelled or when there is no terminal for a menu. Long lists are paged,
    because one menu frame holds a limited number of rows."""
    options = tuple(options)
    page = default // PAGE
    while True:
        start = page * PAGE
        shown = list(options[start:start + PAGE])
        count = len(shown)
        following = len(shown) if start + count < len(options) else None
        if following is not None:
            shown.append(_NEXT)
        previous = len(shown) if page else None
        if previous is not None:
            shown.append(_PREVIOUS)
        try:
            answer = choose(title, shown, default=default - start if start <= default < start + count else 0)
        except MenuInputError:
            return None
        if answer is None:
            return None
        if answer == following:
            page += 1
        elif answer == previous:
            page -= 1
        else:
            return start + answer


class Controls:
    def __init__(self):
        self.skills = None                   # this conversation's choice; None means what the agent has assigned

    def _assigned(self):
        name, profile = self._profile()
        return name, profile, (profile.skills if self.skills is None else self.skills)

    def activate(self):
        """Send only the catalog of Skills with each question. Without an agent to read, the snapshot stays as it was."""
        selected = execution.current()
        if selected is None:
            return
        try:
            _, profile, chosen = self._assigned()
            execution.select(dict(selected, instructions=profiles.catalog_for(replace(profile, skills=tuple(chosen)))))
        except (profiles.ProfileError, OSError, ValueError, KeyError):
            print("Nie można wczytać opisów Skilli. Sprawdź ai-desktop skills validate.")

    def load_skill(self, name):
        """The body of one Skill the model asked for. None for anything outside this conversation's selection."""
        try:
            _, _, chosen = self._assigned()
            return profiles._skill(name) if name in chosen else None
        except (profiles.ProfileError, OSError, ValueError, KeyError):
            return None

    def handle(self, line, conversation=None):
        """False: `line` is an ordinary question. True: handled here. EXIT: the owner ends the conversation."""
        if line[:1].isspace():
            return False                     # a leading space is the documented way to send such a word as a question
        text = line.strip()
        word, _, value = text.partition(" ")
        if not _SHAPE.fullmatch(word) or "\n" in word:
            return False
        value = value.strip()
        name = _ALIASES.get(word, word)
        if name == "/":
            chosen = _pick("Polecenia", tuple("%s  %s" % item for item in COMMANDS))
            if chosen is None:
                self._help()
                return True
            name = COMMANDS[chosen][0]
        if name == "/pomoc":
            self._help()
        elif name == "/koniec":
            return EXIT
        elif name == "/ustawienia":
            self._settings()
        elif name == "/uprawnienia":
            self._permissions()
        elif name == "/model":
            self._choice("/model", "model", value, execution.MODELS if self._provider() == "claude" else (), conversation)
        elif name == "/sila":
            self._choice("/sila", "effort", value, execution.efforts_for(self._provider()), conversation)
        elif name == "/agent":
            self._agent(value, conversation)
        elif name == "/skills":
            self._skills(value, conversation)
        else:
            print("Nieznane polecenie %s. Lista: /pomoc. Aby wysłać to jako pytanie, zacznij od spacji." % word)
        return True

    @staticmethod
    def _help():
        for name, description in COMMANDS:
            print("%-14s %s" % (name, description))
        print("Polecenia działają lokalnie, nic nie wysyłają i nie zmieniają przebiegu rozmowy. Zapis na stałe: ai-desktop agents.")

    @staticmethod
    def _provider():
        selected = execution.current()
        return selected["provider"] if selected is not None else "claude"

    @staticmethod
    def _config():
        config = profiles.Store().load().config
        return config.with_provider(config.selected_provider)

    def _profile(self):
        selected = execution.current()
        config = self._config()
        name = selected["profile"] if selected is not None else config.active
        return name, config.profiles[name]

    def _settings(self):
        run_controls.conversation_command("/ustawienia")
        try:
            _, profile = self._profile()
        except (OSError, ValueError, KeyError):
            return
        chosen = profile.skills if self.skills is None else self.skills
        print("Skills: " + (", ".join(chosen) or "brak"))

    @staticmethod
    def _permissions():
        from . import assistant_flow
        try:
            snapshot = assistant_flow.capture_access()
        except assistant_flow.AccessDenied:
            print("Nie można odczytać permissions.toml. Sprawdź ten plik.")
            return
        assistant_flow.describe_access(snapshot)
        for scope in snapshot.access["files"] + snapshot.access.get("write", []):
            print("  %s: %s" % (scope["id"], scope["pattern"]))

    @staticmethod
    def _rebind(conversation):
        print(_AGAIN)
        if conversation is None:
            return
        try:
            conversation.rebind(execution.current())
        except (SessionError, OSError, ValueError):
            print("Zmiana działa w tej rozmowie, ale nie udało się jej zapisać przy zapisanej rozmowie.")

    @staticmethod
    def _active():
        """What the next question would use: a role is resolved even while the plain default leaves no snapshot."""
        return execution.current() or profiles.resolve()

    def _choice(self, command, field, value, options, conversation):
        try:
            present = self._active()[field]
        except (OSError, ValueError, KeyError):
            present = None
        if not value and options:
            options = tuple(options)
            chosen = _pick("%s · teraz: %s" % (command, present), options,
                           default=options.index(present) if present in options else 0)
            if chosen is None:
                run_controls.conversation_command(command)
                return
            value = options[chosen]
        if value == present:
            print("Bez zmian: %s to już %s." % (command, value))
            return
        before = execution.current()
        run_controls.conversation_command((command + " " + value).strip())
        if value and execution.current() != before:
            self._rebind(conversation)

    def _agent(self, value, conversation):
        if conversation is not None:
            run_controls.conversation_command("/agent x", saved=True)
            return
        if not value:
            try:
                names = tuple(sorted(self._config().profiles))
            except (OSError, ValueError):
                names = ()
            try:
                present = self._profile()[0]
            except (OSError, ValueError, KeyError):
                present = None
            chosen = _pick("/agent · teraz: %s" % present, names,
                           default=names.index(present) if present in names else 0) if names else None
            if chosen is None:
                run_controls.conversation_command("/agent")
                return
            value = names[chosen]
        try:
            if value == self._profile()[0]:
                print("Bez zmian: /agent to już %s." % value)
                return
        except (OSError, ValueError, KeyError):
            pass
        before = execution.current()
        run_controls.conversation_command("/agent " + value)
        if execution.current() != before:
            self.skills = None
            self.activate()
            self._rebind(None)

    def _skills(self, value, conversation):
        from . import skills
        try:
            name, profile = self._profile()
            available = tuple(skills.names())
        except (OSError, ValueError, KeyError):
            print("Nie można odczytać ustawień agenta. Sprawdź ai-desktop agents list.")
            return
        chosen = list(profile.skills if self.skills is None else self.skills)
        if value:
            if value not in available:
                print("Nie ma Skilla %s. Dostępne: %s" % (value, ", ".join(available)))
                return
            chosen = [item for item in chosen if item != value] if value in chosen else chosen + [value]
        else:
            result = self._skill_menu(available, chosen)
            if result is None:
                print("Skills: " + (", ".join(chosen) or "brak") + " · dostępne: " + ", ".join(available))
                print("Przełącz jeden: /skills NAZWA")
                return
            chosen = result
        selected = dict(execution.current() or profiles.resolve(profile=name))
        try:
            selected["instructions"] = profiles.catalog_for(replace(profile, skills=tuple(chosen)))
            execution.select(selected)
        except (profiles.ProfileError, OSError, ValueError):
            print("Nie zmieniono Skilli: nie można ich wczytać albo razem przekraczają %d KiB."
                  % (profiles.MAX_EXECUTION_INSTRUCTIONS // 1024))
            return
        self.skills = tuple(chosen)
        print("Skills: " + (", ".join(chosen) or "brak") + " (ta rozmowa; na stałe: ai-desktop agents).")
        self._rebind(conversation)

    @staticmethod
    def _skill_menu(available, chosen):
        """Enter ticks the highlighted Skill, as does Space; ticks survive changing the page. None: cancelled or no terminal."""
        marks = {index for index, name in enumerate(available) if name in chosen}
        page, position = 0, None
        while True:
            start = page * PAGE
            names = list(available[start:start + PAGE])
            rows = list(names)
            following = len(rows) if start + len(names) < len(available) else None
            if following is not None:
                rows.append(_NEXT)
            previous = len(rows) if page else None
            if previous is not None:
                rows.append(_PREVIOUS)
            apply = len(rows)
            rows.append(_APPLY)
            local = {index - start for index in marks if start <= index < start + len(names)}
            try:
                answer = choose("Skills · spacja zaznacza · wybrane: %d" % len(marks), rows,
                                default=apply if position is None else position, marks=local, markable=len(names))
            except MenuInputError:
                return None
            if answer is None:
                return None
            marks = {index for index in marks if not start <= index < start + len(names)} | {index + start for index in local}
            if answer == apply:
                return [name for index, name in enumerate(available) if index in marks]
            if answer == following:
                page, position = page + 1, None
            elif answer == previous:
                page, position = page - 1, None
            else:
                marks.symmetric_difference_update({answer + start})
                position = answer
