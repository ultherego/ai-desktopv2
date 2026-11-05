---
name: cachyos-kernel
description: Diagnozuje regresje po zmianie jądra CachyOS i zgodność jego modułów. Użyj przy wyborze wariantu, błędach initramfs, DKMS albo potrzebie porównania z działającym jądrem.
---

# Jądro i moduły CachyOS

## Ustal punkt odniesienia

Ustal objaw, ostatnie działające jądro, moment aktualizacji i czy wykonano restart. Odczytaj `uname -r` oraz dozwolone zapytania pakietowe, np. `pacman -Qs linux-cachyos` i `pacman -Qi NAZWA`. Działające jądro może być starsze od zainstalowanego; sama ta różnica przed restartem nie dowodzi awarii.

Nie wybieraj wariantu na podstawie samej obietnicy wydajności. Nazwy, dostępność i funkcje pakietów sprawdzaj w aktualnej dokumentacji i repozytoriach, zamiast zakładać niezmienny katalog wariantów.

## Interpretacja

| Obserwacja | Następny krok |
|---|---|
| Objaw występuje tylko na nowym jądrze | Porównaj te same warunki na zachowanym działającym jądrze; zbierz odpowiedni dziennik. Zbieżność czasowa jest hipotezą regresji. |
| `module not found` przy budowie initramfs | Ustal dokładny moduł i jądro, którego dotyczy błąd; sprawdź pliki właściwego pakietu. Nie usuwaj modułu z konfiguracji tylko po to, by ukryć błąd. |
| Moduł DKMS nie został zbudowany | Sprawdź zgodne nagłówki i konkretny log budowy. Nie zakładaj, że pakiet modułu prekompilowanego pasuje do każdego wariantu. |
| Błąd ładowania lub podpisu modułu | Oddziel niezgodność wersji, brak modułu i politykę podpisów. Nie wyłączaj Secure Boot jako pierwszej naprawy. |
| System nie dochodzi do uruchomienia usług | Zastosuj procedurę rozpoznania rozruchu, np. `cachyos-boot-snapshots`, jeśli jest dostępna. |
| Brak logu albo możliwości porównania | Podaj hipotezę i brakujący dowód; nie deklaruj potwierdzonej regresji. |

Logi jądra odczytuj tylko z właściwego czasu i zakresu udostępnionego przez kontroler. Odmowy nie obchodź przez `sudo` ani swobodną powłokę.

## Zmiana i test

Przed instalacją lub usunięciem ustal zależne moduły, nagłówki dla DKMS oraz mechanizm generowania initramfs i wpisów bootloadera. Zachowaj co najmniej jedno sprawdzone jądro i możliwość wyboru go przy rozruchu. Nie usuwaj działającego wariantu podczas próby.

Przedstaw osobny plan z pakietami, zmianami w `/boot`, zgodą i drogą odzyskania. Po wykonaniu sprawdź wynik hooków i dostępność wpisów, a po kontrolowanym restarcie `uname -r`, stan potrzebnych modułów i pierwotny objaw. Sama instalacja pakietu nie dowodzi uruchomienia nowego jądra.

Cofnięcie wymaga zgodnych plików jądra, initramfs, modułów i konfiguracji. Migawka root może nie obejmować `/boot` lub ESP. Jeśli kontroler nie potrafi bezpiecznie wykonać operacji, zakończ na planie dla użytkownika.

## Wynik

Podaj jądro działające i zainstalowane, stan istotnego modułu, dowód diagnozy, wykonany test i jeden następny krok. Nie obiecuj poprawy wydajności bez porównania tego samego obciążenia.

## Źródła

- [Warianty i moduły prekompilowane](https://wiki.cachyos.org/features/kernel/#variants).
- [Zakres przywracania migawek](https://wiki.cachyos.org/configuration/btrfs_snapshots/).
