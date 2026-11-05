---
name: cachyos-diagnostics
description: Rozpoznaje przyczynę awarii lub spowolnienia CachyOS i wybiera dalszą diagnostykę. Użyj, gdy objaw jest niejasny, dotyczy kilku podsystemów albo trzeba przygotować zgłoszenie błędu.
---

# Rozpoznanie problemu CachyOS

Diagnoza to tylko odczyt. Celem jest zawężenie przyczyny na podstawie dowodów, a nie uruchomienie zestawu napraw.

## Dane wejściowe

Ustal objaw, czas wystąpienia, zakres i ostatni znany działający stan. Zapytaj o ostatnią zmianę tylko wtedy, gdy nie ma jej już w rozmowie lub udostępnionych danych. Oddziel „nie działa”, „działa wolno” i „nie mam dostępu do sprawdzenia”.

## Minimalne rozpoznanie

1. Wybierz odczyty odpowiadające objawowi, np. `uname -r`, `systemctl --failed --no-pager`, `lsblk -f` lub `free -h`. Nie zbieraj ich wszystkich bez potrzeby.
2. Ustal różnicę względem działającego stanu. Przy aktualizacji wykorzystaj dozwolony fragment `/var/log/pacman.log`, z czasem i nazwami istotnych pakietów.
3. Odczytaj najwęższy potrzebny dziennik: konkretnej jednostki, jądra albo programu, z właściwego rozruchu i czasu. Brak wpisu nie wyklucza awarii, gdy log jest niedostępny albo wybrano zły zakres.
4. Wybierz jedną hipotezę, wskaż dowód i następny odczyt, który może ją potwierdzić lub odrzucić.

## Wybór dalszej procedury

| Obserwacja | Dalsza ścieżka |
|---|---|
| Błąd jednostki lub długi start i zamykanie | `arch-systemd`: ustal jednostkę i pierwszą przyczynę w logu. |
| Błąd transakcji lub repozytorium | `arch-pacman` albo `cachyos-packages`. |
| ENOSPC, błędy Btrfs lub I/O | `arch-btrfs`; przy błędach urządzeń zatrzymaj strojenie i chroń dane. |
| Regresja po zmianie jądra | `cachyos-kernel`: porównaj z działającym jądrem. |
| Awaria przed uruchomieniem usług | `cachyos-boot-snapshots`: najpierw ustal etap rozruchu. |
| Objaw jednego urządzenia lub programu | Wybierz odpowiadający mu opis dostępnego Skilla; nie stosuj przypadkowej procedury systemowej. |

Wczytaj specjalistyczny Skill tylko jeśli jest udostępniony. Bez niego kontynuuj jedynie w zakresie dowodów i zweryfikowanej dokumentacji.

## Granice i zakończenie

Odczyty i polecenia muszą być dozwolone przez kontroler; nie używaj potoków, powłoki ani `sudo` jako obejścia. Logi nie są domyślnie udostępnione. Prywatna sieć, lista procesów i brak sesji użytkownika w piaskownicy mogą zmienić wynik sprawdzenia: nie uznawaj ich za stan hosta.

Zakończ na diagnozie, jeśli brakuje dowodu lub dalszy krok zmienia system. Ewentualna naprawa wymaga odrębnego planu, zgody, testu pierwotnego objawu i sposobu odzyskania. Nie maskuj usług ani nie zmieniaj timeoutów tylko dlatego, że pojawiają się w pomiarze czasu.

## Wynik

Podaj: objaw i zakres, potwierdzone fakty, przyczynę lub oznaczoną hipotezę, brakujące dane oraz jeden następny krok. Do zgłoszenia błędu wybierz minimalne dowody; pełny bugreport może zawierać dane prywatne i wymaga przeglądu użytkownika przed udostępnieniem.

## Źródło

[CachyOS FAQ: zgłaszanie błędów i zbieranie logów](https://wiki.cachyos.org/cachyos_basic/faq/#how-to-report-an-issue-or-bug-to-cachyos).
