"""Small local conversation manager, separate from the temporary AI worker."""
import argparse
import sys
import time
import unicodedata

from .conversation_sessions import SessionError, Store
from .run_controls import recipient
from .terminal_menu import MenuInputError, choose

PAGE_SIZE = 8
EMPTY = "Brak zapisanych rozmów. Program zapyta o zapis przy wyjściu z rozmowy (albo użyj --session new)."
MANAGE_PAGE = 9                  # nine threads + up to three actions fit the twelve rows of one menu


def build_parser():
    parser = argparse.ArgumentParser(prog="ai-desktop sessions",
                                     description="Lokalne rozmowy zapisane przez --session; bez uruchamiania AI")
    commands = parser.add_subparsers(dest="action")
    commands.add_parser("list", help="Pokaż zapisane rozmowy")
    commands.add_parser("menu", help="Wybierz rozmowę i działanie strzałkami")
    for name, description in (("show", "Pokaż ostatnie pytania i odpowiedzi"),
                              ("resume", "Wznów rozmowę z nową kontrolą dostępu i zgodą"),
                              ("remove", "Usuń zapis rozmowy")):
        command = commands.add_parser(name, help=description)
        command.add_argument("id", help="Identyfikator z sessions list")
    return parser


def _values(store):
    return sorted(store.list(), key=lambda item: (item["updated"], item["id"]), reverse=True)


def _safe(value, limit=1600):
    """Render bounded local record text without terminal control sequences."""
    if type(value) is not str:
        raise SessionError("session-text")
    visible = "".join(c if c in "\n\t" or unicodedata.category(c) not in ("Cc", "Cf", "Cs", "Zl", "Zp")
                      else "�" for c in value[:limit])
    return visible + (" …[skrócono]" if len(value) > limit else "")


def _entry(value, active=False):
    profile = value["execution"]["profile"] if value["execution"] is not None else "default"
    return "%s · %s · %s · %s · %d pytań%s" % (
        value["id"], value["kind"], recipient(config=value["execution"]), profile,
        len(value["history"]), " [aktywna]" if active else "")


def thread_label(value, active=False):
    """One line for a saved thread: when it was last used, its title (the user's first message) and its size."""
    first = value["history"][0]["question"] if value["history"] else value["id"]
    title = value.get("title") or " ".join(first.split())
    title = title if len(title) <= 60 else title[:59].rstrip() + "…"
    stamp = time.strftime("%d.%m %H:%M", time.localtime(value["updated"]))
    return "%s · %s · %d wymian%s" % (stamp, _safe(title, 80).replace("\n", " "), len(value["history"]),
                                     " [aktywna]" if active else "")


def _manage(store):
    """Tick threads with Space (they stay ticked across pages), then go down to 'Usuń zaznaczone' and confirm."""
    marked, page = set(), 0
    while True:
        values = _values(store)
        if not values:
            print(EMPTY)
            return 0
        pages = (len(values) + MANAGE_PAGE - 1) // MANAGE_PAGE
        page = min(page, pages - 1)
        visible = values[page * MANAGE_PAGE:(page + 1) * MANAGE_PAGE]
        local = {index for index, value in enumerate(visible) if value["id"] in marked}
        options = [thread_label(value, store.active(value["id"])) for value in visible]
        actions = ["Usuń zaznaczone (%d)" % len(marked)]
        if page:
            actions.append("Poprzednia strona")
        if page + 1 < pages:
            actions.append("Następna strona")
        actions.append("Wróć")
        choice = choose("Zapisane rozmowy · strona %d/%d" % (page + 1, pages), options + actions,
                        marks=local, markable=len(visible))
        marked -= {value["id"] for value in visible}
        marked |= {visible[index]["id"] for index in local}
        if choice is None:
            return 0
        if choice < len(visible):
            marked ^= {visible[choice]["id"]}              # Enter on a thread ticks it as well
            continue
        action = actions[choice - len(visible)]
        if action == "Wróć":
            return 0
        if action == "Poprzednia strona":
            page -= 1
        elif action == "Następna strona":
            page += 1
        elif not marked:
            print("Nic nie zaznaczono.")
        else:
            decision = choose("Usunąć %d zaznaczonych rozmów? Tego nie można cofnąć." % len(marked),
                              ("Nie, wróć", "Tak, usuń zaznaczone"))
            if decision == 1:
                removed = skipped = 0
                for ident in sorted(marked):
                    try:
                        store.remove(ident, expected=store.read(ident))
                        removed += 1
                    except SessionError:
                        skipped += 1                          # active in another process, or changed meanwhile
                marked.clear()
                print("Usunięto rozmów: %d%s." % (removed, ", pominięto (aktywne lub zmienione): %d" % skipped if skipped else ""))


def select_thread(store, execution_config):
    """Start of `ask`: ('new', None), ('resume', id) or None (cancelled). Only threads of this agent are offered."""
    from . import execution
    wanted = execution.validate(execution_config)
    while True:
        values = [value for value in _values(store) if value["kind"] == "ask" and value["execution"] == wanted]
        if not values:
            return "new", None
        recent = values[:7]
        options = ["Nowa rozmowa"] + [thread_label(value, store.active(value["id"])) for value in recent] \
            + ["Zarządzaj zapisanymi rozmowami"]
        choice = choose("Rozmowa", options)
        if choice is None:
            return None
        if choice == 0:
            return "new", None
        if choice == len(options) - 1:
            _manage(store)
            continue
        return "resume", recent[choice - 1]["id"]


def _pick(title, options):
    selected = choose(title, options)
    if selected is not None and (type(selected) is not int or not 0 <= selected < len(options)):
        raise MenuInputError("menu-selection")
    return selected


def _show(value, *, preview=False):
    print("Rozmowa %s · %s · %s" % (value["id"], value["kind"], recipient(config=value["execution"])))
    if value["path"]:
        print("Kontekst:", _safe(value["path"], 400 if preview else 4096))
    if value["execution"] is not None:
        selected = value["execution"]
        print("Agent: %s · model: %s · rozumowanie: %s" % (
            selected["profile"], selected["model"], selected["effort"]))
    history = value["history"][-1:] if preview else value["history"]
    for item in history:
        print("\n> " + _safe(item["question"], 500 if preview else 2048))
        print(_safe(item["answer"], 1600 if preview else 8192 + 64))
    if not history:
        print("Brak zakończonych pytań.")
    if preview:
        print("Pełna historia: ai-desktop sessions show " + value["id"])


def _resume(store, ident, dispatch):
    resume = store.resume_args(ident)
    if dispatch is None:
        from .cli import main as dispatch
    return dispatch(resume)


def _menu(store, dispatch):
    """Select only existing records; execution still uses fresh resume checks."""
    page = 0
    while True:
        values = _values(store)
        if not values:
            print(EMPTY)
            return 0
        pages = (len(values) + PAGE_SIZE - 1) // PAGE_SIZE
        page = min(page, pages - 1)
        visible = values[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
        options = [_entry(value, store.active(value["id"])) for value in visible]
        actions = []
        if page:
            actions.append("Poprzednia strona")
        if page + 1 < pages:
            actions.append("Następna strona")
        actions.append("Zaznacz i usuń wiele rozmów")
        actions.append("Wróć")
        selected = _pick("Zapisane rozmowy · strona %d/%d" % (page + 1, pages), options + actions)
        if selected is None:
            return 0
        if selected >= len(visible):
            action = actions[selected - len(visible)]
            if action == "Wróć":
                return 0
            if action == "Zaznacz i usuń wiele rozmów":
                _manage(store)
                continue
            page += -1 if action == "Poprzednia strona" else 1
            continue
        ident = visible[selected]["id"]
        # Capture a fresh complete record for the reviewed deletion. A later
        # update must be refused by Store.remove under its existing lock.
        reviewed = store.read(ident)
        while True:
            selected = _pick(_entry(reviewed, store.active(ident)),
                             ("Podgląd ostatniego pytania i odpowiedzi", "Wznów z nową kontrolą dostępu i zgodą",
                              "Usuń zapis tej rozmowy", "Wróć"))
            if selected is None or selected == 3:
                break
            if selected == 0:
                _show(reviewed, preview=True)
            elif selected == 1:
                if store.active(ident):
                    raise SessionError("session-active")
                return _resume(store, ident, dispatch)
            else:
                store.remove(ident, expected=reviewed)
                print("Usunięto zapis rozmowy.")
                break


def main(argv=None, *, store=None, dispatch=None):
    args = build_parser().parse_args(argv)
    store = Store() if store is None else store
    try:
        if args.action == "menu" or (args.action is None and sys.stdin.isatty() and sys.stdout.isatty()):
            return _menu(store, dispatch)
        if args.action in (None, "list"):
            values = _values(store)
            if not values:
                print(EMPTY)
            for value in values:
                active = store.active(value["id"])
                profile = value["execution"]["profile"] if value["execution"] is not None else "default"
                print("%s  %s  %s  %s  %d pytań%s" % (
                    value["id"], value["kind"], recipient(config=value["execution"]), profile,
                    len(value["history"]), "  [aktywna]" if active else ""))
            for ident in store.damaged():
                print("%s  uszkodzony zapis; usuń: ai-desktop sessions remove %s" % (ident, ident))
            return 0
        if args.action == "show":
            value = store.read(args.id)
            _show(value)
            return 0
        if args.action == "remove":
            if args.id in store.damaged():
                store.remove_damaged(args.id)
            else:
                store.remove(args.id)
            print("Usunięto zapis rozmowy.")
            return 0
        return _resume(store, args.id, dispatch)
    except (MenuInputError, EOFError, KeyboardInterrupt):
        print("Nie odczytano wyboru rozmowy. Użyj interaktywnego terminala lub ai-desktop sessions list.")
        return 2
    except (SessionError, OSError, ValueError, TypeError, KeyError) as exc:
        reason = str(exc) if isinstance(exc, SessionError) else "session-file"
        if reason == "session-active":
            print("Rozmowa jest aktywna w innym procesie. Zakończ ją przed wznowieniem lub usunięciem.")
        elif reason == "session-binding":
            print("Plik, projekt lub ustawienia agenta zmieniły się. Rozpocznij nową rozmowę przez --session new.")
        elif reason == "session-id":
            print("Niepoprawny identyfikator rozmowy. Sprawdź ai-desktop sessions list.")
        elif reason == "session-conflict":
            print("Rozmowa zmieniła się podczas wyboru. Wybierz ją ponownie.")
        else:
            print("Nie można odczytać lub zapisać rozmowy. Sprawdź zapis i uprawnienia katalogu sesji.")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
