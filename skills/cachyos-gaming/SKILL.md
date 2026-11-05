---
name: cachyos-gaming
description: Diagnozuje uruchamianie, awarie i wydajność gier na CachyOS. Użyj przy Steam, Protonie, Lutris lub Heroic; prowadzi porównanie jednej zmiany z wynikiem bazowym.
---

# Diagnostyka gry i pomiar zmiany

## Zbierz minimalny kontekst

Ustal grę, sposób instalacji i uruchamiania, wersję gry i warstwy zgodności, kartę oraz sterownik. Zapytaj o dokładny objaw i ostatnią zmianę, jeśli nie wynika to z rozmowy. Rozróżnij brak uruchomienia, awarię po czasie i gorszą wydajność. Nie zaczynaj od strojenia systemu.

## Wybierz ścieżkę

| Obserwacja | Następny krok |
|---|---|
| Gra nie uruchamia się | Sprawdź komunikat, wymagania gry i obsługę zabezpieczeń w źródłach gry; porównaj wersję warstwy zgodności. |
| Kończy się jeden konkretny tytuł | Odczytaj log z tej próby i dopasuj czas błędu; odróżnij istotny błąd od powtarzalnych ostrzeżeń. |
| Problemy dotyczą wielu gier | Sprawdź wspólny sterownik, zasoby, bibliotekę gier i ostatnią aktualizację; skorzystaj z właściwego wybranego Skilla. |
| Spada płynność po czasie | Porównaj obciążenie, temperatury i pamięć z momentem wystąpienia objawu. Samo użycie swap nie dowodzi przyczyny. |
| Objaw występuje tylko z nakładką lub nagrywaniem | Zaplanuj próbę bez jednego dodatku, zachowując pozostałe ustawienia. |

Odczyt plików i logów wymaga dostępu w polityce. Piaskownica poleceń nie widzi zwykle sesji gry; brak urządzenia lub procesu w jej widoku nie dowodzi braku na hoście. Nie czytaj danych konta i nie proś o cały katalog gry lub pełny raport bez potrzeby.

## Próba i porównanie

1. Zapisz stan bazowy: ustawienia grafiki, rozdzielczość, limit FPS, wersję warstwy zgodności, opcje startu oraz odtwarzalną scenę. Wybierz miarę odpowiadającą celowi: awaria, czas uruchomienia, czas klatki, FPS lub pobór mocy.
2. Dobierz jedną zmianę wynikającą z dowodów. Zachowaj poprzednią wartość i sposób jej przywrócenia. Nie zmieniaj globalnie kilku parametrów ani nie usuwaj prefiksu lub cache jako zwykłego testu.
3. Po osobnej zgodzie na zmianę wykonaj tę samą próbę w porównywalnych warunkach. Oddziel pierwszy przebieg z kompilacją shaderów od kolejnych; porównaj kilka powtórzeń. Jedna korzystna liczba bez warunków pomiaru nie dowodzi poprawy.
4. Przy awarii lub pogorszeniu zaproponuj przywrócenie dokładnego stanu bazowego. Nie utrwalaj zmiany na podstawie niepełnego pomiaru.

Do uzasadnionej próby diagnostycznej Proton udostępnia opcję startu Steam `PROTON_LOG=1 %command%`. Jej ustawienie i utworzenie logu są zmianą; nie udawaj ich wykonania przez aplikację. Przed odczytem wybierz potrzebny fragment, a po próbie przywróć wcześniejsze opcje. Dostępność parametrów sprawdzaj w dokumentacji używanej wersji.

## Wynik

Podaj zakres problemu, dowody, tabelę baza/próba z warunkami pomiaru, stan zastosowania i wynik. Jeśli próba nie odbyła się na żywej grze, nazwij ją niewykonaną. Nie obiecuj wzrostu wydajności na podstawie nazwy pakietu lub planisty.

## Źródła

- CachyOS, logi i opcje uruchamiania: <https://wiki.cachyos.org/configuration/gaming/>.
- Proton, parametry runtime: <https://github.com/ValveSoftware/Proton#runtime-config-options>.
