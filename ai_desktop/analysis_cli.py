"""One useful read-only flow: preview -> explicit consent -> analysis -> text."""
import json
import os
import sys

from . import diagnose
from .analysis_contract import (AnalysisConsent, AnalysisDenied, CLIENT_CONTEXT, CLIENT_VERSION,
                                POLICY, SYSTEM_PROMPT, MAX_QUESTION_BYTES, validate_question)
from .analysis_service import send_once
from .analysis_session import installation_ready, managed_session
from .analysis_terminal import render_answer
from .compare import read_snapshot
from .report_policy import serialize_report
from .terminal_consent import confirm, fresh_input
from .run_controls import recipient, failure_hint, label, wait_message
from .execution import UNSET
from .terminal_style import rule

ANSWER_SEPARATOR = "─" * 48

HINTS = {
    "network": "Nie udalo sie nawiazac dozwolonego polaczenia. Sprawdz siec i dostepnosc uslugi; potem uruchom polecenie ponownie i zatwierdz wysylke.",
    "rate-limit": "Usluga odrzucila zadanie z powodu limitu. Odczekaj i uruchom polecenie ponownie z nowa zgoda; bez automatycznej ponownej wysylki.",
    "provider-unavailable": "Usluga Claude zglosila chwilowy blad. Sprobuj pozniej, uruchamiajac polecenie ponownie z nowa zgoda.",
    "runtime": "Brak zaufanego klienta Claude Code 2.1.312 w pakiecie ai-desktop.",
    "version": "Klient lub izolacja nie przeszly kontroli wersji 2.1.312.",
    "endpoint": "Nie udalo sie uruchomic lub polaczyc sesji. Sprawdz ai-desktop status; odzyskanie: ai-desktop stop.",
    "auth-required": "Połącz konto: ai-desktop login (wybierz Claude). Potem uruchom nową analizę i zatwierdź nowy podgląd.",
    "auth-method": "Wymagane logowanie subskrypcji claude.ai w osobnym profilu; inne metody sa odrzucone.",
    "busy": "Sesja lub profil jest zajety albo pozostal niedokonczony cykl. Sprawdz ai-desktop status; odzyskanie wlasnej sesji: ai-desktop stop.",
    "timeout": "Przekroczono limit czasu. Wynik nie jest potwierdzony; brak automatycznego ponowienia.",
    "client": "Klient nie zakonczyl analizy. Sprawdz dostep do sieci i stan subskrypcji. Brak automatycznego ponowienia i przejscia na API.",
    "consent": "Nie zatwierdzono wysylki lub zgoda wygasla. Przy nowej analizie sprawdz podglad i wpisz tak (t), aby zatwierdzic, albo nie (n), aby odmowic. Raport nie zostal przekazany.",
    "authorization": "Nie uzyskano uprawnien do uruchomienia osobnej sesji. Raport nie zostal przekazany.",
    "cleanup": "Nie potwierdzono zatrzymania sesji. Uruchom ai-desktop stop; nie ponawiaj automatycznie wysylki.",
    "cancelled": "Przerwano analize. Wyslanych danych nie mozna wycofac; brak automatycznego ponowienia.",
}


def main(*, preview=False, question="", ask=False, report_path=None, output_format="auto", details=False, reader=input):
    submitted = False
    shown_execution = UNSET
    try:
        if os.geteuid() == 0 or os.getuid() != os.geteuid():
            raise AnalysisDenied("identity")
        if (not preview or ask) and (not sys.stdin.isatty() or not sys.stdout.isatty()):
            raise AnalysisDenied("consent")
        if (type(details) is not bool or output_format not in ("auto", "plain", "markdown")
                or (report_path is not None and (type(report_path) is not str or report_path == "-"))
                or (ask and question)):
            raise AnalysisDenied("input")
        validate_question(question)
        if not preview:
            installation_ready()
        report = read_snapshot(report_path) if report_path is not None else diagnose.collect()
        if ask:
            prompt = "Pytanie o raport (Enter = ogolna analiza): "
            question = fresh_input(prompt, max_bytes=MAX_QUESTION_BYTES) if reader is input else reader(prompt)
        gate = AnalysisConsent(report, question=question)
        envelope = json.loads(gate.payload)
        # Show the recipient of these exact prepared bytes. Account login does
        # not select a provider, and later ambient settings must not relabel
        # the preview or the answer/error of an already prepared request.
        shown_execution = envelope.get("execution")
        print(label(config=shown_execution))
        print("=== RAPORT DO ANALIZY ===" if details else "Raport do analizy:")
        print(serialize_report(envelope["report"]))
        print("=== TWOJE PYTANIE ===" if details else "Pytanie:")
        print(question if question else "Ogólna analiza raportu.")
        if details:
            print("=== INSTRUKCJA APLIKACJI ===")
            print(envelope["system_prompt"])
            print("=== ZASADY I ODBIORCA ===")
            print(envelope["client_context"])
            print("Klient:", envelope["client_version"], "Zasady:", envelope["policy"])
            print("SHA256 raportu, pytania, instrukcji i zasad:", gate.sha256)
        else:
            print(recipient("Do Claude trafią powyższe dane i stała instrukcja aplikacji. Wynik nie wykonuje zmian.",
                            config=shown_execution))
        if preview:
            if details:
                print("PREVIEW: PASS (offline; bez uruchamiania klienta i dostepu do profilu)")
            else:
                print("Podgląd offline — niczego nie wysłano.")
            return 0
        if not confirm("transfer", reader=reader, config=shown_execution):
            if details:
                raise AnalysisDenied("consent")
            print("Anulowano. Raport nie został wysłany.")
            return 2
        gate.check(report, question=question, sha256=gate.sha256, nonce=gate.nonce)
        with managed_session(**({} if details else {"quiet": True})):
            # sudo/setup can take time: recheck the same immutable preview and
            # TTL immediately before transfer; never automatically renew consent.
            approval = gate.authorize(report, question=question, sha256=gate.sha256, nonce=gate.nonce)
            waiting = wait_message(config=shown_execution)
            if details:
                waiting += " Wysłanego raportu nie można wycofać."
            print(waiting, flush=True)
            submitted = True
            result = send_once(approval)
        print("=== WYNIK " + recipient(config=shown_execution).upper() + " — propozycje do oceny, bez wykonania zmian ===" if details
              else "\n" + rule() + "\n")
        print(render_answer(result, output_format=output_format))
        if details:
            print("ANALYSIS: PASS sha256=" + gate.sha256)
            print("SESSION: STOPPED")
        else:
            print("\n" + rule() + "\n")
        return 0
    except AnalysisDenied as exc:
        if details:
            print(str(exc))
        print(failure_hint(exc.reason, HINTS.get(exc.reason, "Analiza zatrzymana przez brame bezpieczenstwa; brak zmian w systemie."),
                           config=shown_execution,
                           **({"diagnostic": exc.diagnostic} if exc.diagnostic is not None else {})))
        return 2
    except (EOFError, KeyboardInterrupt):
        if details:
            print("ANALYSIS: CANCELLED (brak automatycznego ponowienia; wyslanych danych nie mozna wycofac)")
        else:
            print("Anulowano.")
            if submitted:
                print("Wysłanych danych nie można wycofać; analiza nie zostanie ponowiona automatycznie.")
        return 2
    except (OSError, ValueError, TypeError):
        print("ANALYSIS: REFUSED reason=input" if details
              else "Nie udało się przygotować analizy. Sprawdź raport i pytanie.")
        return 2
