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
_AGAIN = "Następne pytanie poprosi o zgodę jeszcze raz, bo zmieniły się ustawienia. Rozmowa trwa dalej."


def _pick(title, options, default=0):
    """None when cancelled or when there is no terminal for a menu."""
    try:
        return choose(title, options, default=default)
    except MenuInputError:
        return None


class Controls:
    def __init__(self):
        self.skills = None                   # this conversation's choice; None means what the agent has assigned

    def handle(self, line, conversation=None):
        """False: `line` is an ordinary question. True: handled here. EXIT: the owner ends the conversation."""
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
            self._choice("/model", value, execution.MODELS if self._provider() == "claude" else (), conversation)
        elif name == "/sila":
            self._choice("/sila", value, execution.efforts_for(self._provider()), conversation)
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

    def _choice(self, command, value, options, conversation):
        if not value and options:
            chosen = _pick(command, tuple(options))
            if chosen is None:
                run_controls.conversation_command(command)
                return
            value = options[chosen]
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
            chosen = _pick("/agent", names) if names else None
            if chosen is None:
                run_controls.conversation_command("/agent")
                return
            value = names[chosen]
        before = execution.current()
        run_controls.conversation_command("/agent " + value)
        if execution.current() != before:
            self.skills = None
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
            selected["instructions"] = profiles.instructions_for(replace(profile, skills=tuple(chosen)))
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
        """Enter ticks the highlighted Skill, as does Space; the last row applies. None: cancelled or no terminal."""
        marks = {index for index, name in enumerate(available) if name in chosen}
        options = tuple(available) + ("Zastosuj",)
        position = len(available)
        while True:
            try:
                answer = choose("Skills · spacja zaznacza", options, default=position, marks=marks, markable=len(available))
            except MenuInputError:
                return None
            if answer is None:
                return None
            if answer == len(available):
                return [name for index, name in enumerate(available) if index in marks]
            marks.symmetric_difference_update({answer})
            position = answer
