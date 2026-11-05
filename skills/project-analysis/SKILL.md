---
name: project-analysis
description: Odtwarza architekturę, przepływy danych i stan działania udostępnionego projektu bez zmian. Użyj przy poznawaniu repozytorium, ocenie implementacji i porównaniu kodu z wymaganiami.
---

# Analiza projektu

## Ustal pytanie i pokrycie

Określ cel analizy: jak projekt działa, czy spełnia wymaganie, gdzie powstaje problem lub co blokuje ukończenie. Ustal wersję, jeśli jest dostępna, oraz granice udostępnionych plików. Listę nazw traktuj jako mapę do odczytu, nie dowód zachowania.

## Odtwórz działanie

1. Zacznij od dokumentacji celu, manifestów zależności, punktów wejścia i konfiguracji. Porównaj deklaracje z kodem; rozbieżność opisz wprost.
2. Wybierz najważniejszy scenariusz użytkownika. Prześledź go od wejścia przez moduły, przechowywanie danych i zależności do wyniku. Wskaż granice uprawnień oraz skutki uboczne.
3. Dla zewnętrznej zależności ustal interfejs, obsługę błędu i zachowanie przy niedostępności. Dla zapisu ustal zgodę, moment trwałości oraz odzyskiwanie po częściowym wykonaniu.
4. Przeczytaj testy tego przepływu. Rozdziel test jednostki, integrację, atrapę zależności i potwierdzenie na docelowym hoście. Nie uznawaj samej obecności testu za dowód jego przejścia.

## Dobierz następny odczyt

- Gdy nie znasz wywołania komponentu, odczytaj punkt wejścia i jego użytkownika zamiast analizować plik w oderwaniu.
- Gdy dokumentacja obiecuje funkcję, znajdź wykonującą ją ścieżkę i warunki odmowy. Jeśli jej nie ma w dostępnym zakresie, oznacz brak dowodu; nie ogłaszaj automatycznie braku funkcji.
- Gdy limit lub polityka blokuje odczyt, zakończ wniosek dla dostępnej części. Podaj konkretny brak, bez obchodzenia odmowy i bez prośby o cały katalog domowy.
- Gdy analiza dotyczy kolejnej wersji, skup się na zmienionym przepływie i jego zależnościach. Poszerz zakres dopiero przy wykryciu wspólnego problemu.

## Wynik

Oddaj krótki opis celu, mapę odpowiedzialności modułów, przebieg kluczowego scenariusza i ocenę wymagań: potwierdzone w kodzie, potwierdzone wykonaniem, niepełne albo nieweryfikowalne. Każde istotne twierdzenie powiąż z plikiem lub wynikiem kontrolera.

Wskaż najważniejsze ryzyka i jeden uzasadniony następny krok. Szczegółowe defekty opisuj przez warunek, skutek i miejsce; pełny przegląd poprawki należy do `code-review`, jeśli jest wybrany. Nie zmieniaj plików i nie przedstawiaj planowanego testu jako wykonanego. Nie deklaruj pełnej analizy, gdy część projektu pozostała nieodczytana.
