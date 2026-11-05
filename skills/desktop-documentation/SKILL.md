---
name: desktop-documentation
description: Diagnozuje konfigurację pulpitu lub kompozytora na podstawie jego wersji i dokumentacji. Użyj przy KDE, GNOME, Hyprland, Sway, niri, Xfce i innych środowiskach, bez zakładania konkretnego pulpitu.
---

# Konfiguracja pulpitu z dowodami

## Rozpoznaj środowisko

Ustal nazwę i wersję działającego pulpitu lub kompozytora, typ sesji oraz dokładny objaw. Oprzyj się na dostępnych danych sesji, wersji programu lub odpowiedzi użytkownika; sam zainstalowany pakiet nie dowodzi, że obecnie działa. Sprawdź, czy problem dotyczy konfiguracji, urządzenia wyjściowego, pojedynczej aplikacji czy startu całej sesji.

Polecenia uruchamiane przez AI Desktop nie mają zwykle sesji graficznej ani gniazd użytkownika. Błąd połączenia z IPC, Wayland lub usługą użytkownika oznacza wtedy brak widoczności. Nie traktuj go jako dowodu awarii i nie proponuj restartu. Gdy taka obserwacja jest niezbędna, wskaż dokładnie, czego brakuje; nie zmieniaj uprawnień ani trybu wykonania, aby obejść ograniczenie.

## Dobierz dokumentację i plik

1. Wybierz dokumentację właściwego programu i zainstalowanej wersji. Opcja z bieżącej gałęzi dokumentacji może nie istnieć w starszym wydaniu.
2. Odczytaj rzeczywistą konfigurację i jej dołączane pliki tylko w dozwolonym zakresie. Ustal, czy plik jest generowany przez inne narzędzie. Poprawka w pliku generowanym może zniknąć przy następnym zapisie.
3. Przy błędzie składni znajdź wskazany wiersz i definicję opcji. Przy błędzie zachowania sprawdź kolejność reguł oraz warunki dopasowania. Nie przenoś składni między kompozytorami.
4. Jeśli dokumentacja online jest niedostępna, wykorzystaj przekazaną dokumentację lokalną. Bez obu oznacz proponowaną opcję jako niezweryfikowaną; nie przedstawiaj pamięci modelu jako aktualnego źródła.

## Zmiana i test

Zaproponuj najmniejszą zmianę dotyczącą objawu i zachowaj poprzednią zawartość. Zapis wymaga diffu i zgody kontrolera. Użyj walidatora właściwego programu tylko wtedy, gdy dokładne polecenie jest dozwolone i sprawdzi właściwy plik; np. niri udostępnia `niri validate`. Check `text` AI Desktop potwierdza tylko UTF-8, nie poprawność konfiguracji KDL ani reguł pulpitu.

Zaliczony parser potwierdza składnię, nie działanie skrótu, monitora czy reguły. Odtwórz pierwotny objaw po bezpiecznym zastosowaniu i porównaj wynik. Przeładowanie lub restart sesji to osobna zmiana stanu; nie kończ sesji użytkownika w ramach diagnostyki. Przy cofnięciu przywróć wyłącznie własną zmianę, zachowując późniejsze edycje.

## Wynik

Podaj środowisko i wersję, plik oraz użyte źródło, dowód przyczyny lub hipotezę, zakres sprawdzenia i stan zapisu. Gdy blokuje Cię brak widoczności sesji lub walidatora, zakończ na tej konkretnej luce.

## Dokumentacja

- niri: <https://github.com/niri-wm/niri/blob/main/docs/wiki/Configuration:-Introduction.md>.
- Sway, składnia: <https://man.archlinux.org/man/sway.5.en>.
- Hyprland, wybór wersji i działu konfiguracji: <https://wiki.hypr.land/>.
- Pozostałe: <https://docs.kde.org/stable/en/plasma-desktop/>, <https://help.gnome.org/gnome-help/>, <https://docs.xfce.org/>; wybierz dział i wersję odpowiadające zadaniu.
