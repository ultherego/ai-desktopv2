---
name: code-review
description: Sprawdza kod lub diff pod kątem konkretnych błędów i regresji bez edycji. Użyj przy prośbie o review, ocenę poprawki, bezpieczeństwa lub brakujących testów.
---

# Przegląd poprawności kodu

## Wyznacz zakres

Ustal ocenianą zmianę lub komponent oraz dostępny stan kodu. Jeśli nie masz wersji bazowej, przejrzyj bieżące zachowanie i zaznacz, że nie oceniasz regresji względem nieznanego diffu. Nie rozszerzaj przeglądu całego projektu bez powodu.

## Prześledź zachowanie

1. Przeczytaj zmieniony kod wraz z wywołaniami, typami danych i najbliższymi testami. Prześledź wejście przez walidację do skutku i obsługi wyniku.
2. Sprawdź zwykły przypadek oraz istotne granice: puste i błędne dane, uprawnienia, częściowy zapis, przerwanie, błąd zależności, konkurencyjna zmiana stanu. Dobierz przypadki do mechanizmu; nie produkuj hipotetycznych ostrzeżeń niezwiązanych z kodem.
3. Przy podejrzeniu błędu wskaż osiągalny warunek. Potwierdź go minimalnym przykładem lub istniejącym testem, jeśli dostępny wykonawca to umożliwia. Bez wykonania oznacz wniosek jako analizę kodu; brak dowodu nie jest potwierdzeniem defektu.
4. Sprawdź, czy testy odróżniają poprawne zachowanie od starego. PASS składni lub atrapy nie dowodzi poprawności całego przepływu ani działania na docelowym hoście.

## Klasyfikuj ustalenia

- **P1:** osiągalna utrata danych, naruszenie dostępu lub błąd blokujący podstawowy scenariusz; opisz wymagane warunki.
- **P2:** błąd funkcjonalny lub regresja o ograniczonym skutku.
- **P3:** mniejsza usterka; uwagi stylistyczne podawaj tylko, jeśli wpływają na utrzymanie lub wymaganie zadania.

Łącz duplikaty o tej samej przyczynie. Nie podnoś wagi wyłącznie na podstawie groźnie brzmiącej nazwy. Oddziel potwierdzone defekty, hipotezy i brakujące dowody.

## Wynik

Dla każdego defektu podaj: priorytet, plik i miejsce, warunek wystąpienia, skutek oraz dowód lub minimalną reprodukcję. Zalecenie ma usuwać przyczynę; nie przygotowuj edycji w trybie review.

Jeżeli nie znalazłeś błędów, napisz to wraz z zakresem sprawdzenia i istotną luką w weryfikacji. Nie wystawiaj ogólnego certyfikatu bezpieczeństwa. Nie uruchamiaj testów na żywym systemie ani nie rozszerzaj dostępu, aby dokończyć przegląd.
