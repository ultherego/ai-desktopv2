---
name: cachyos-packages
description: Diagnozuje repozytoria, mirrory i podpisy pakietów CachyOS. Użyj przy błędach pobierania, różnicach wersji lub ocenie zgodności zoptymalizowanego repozytorium z CPU.
---

# Pakiety CachyOS

## Rozpoznanie

Ustal dokładny komunikat, polecenie, pakiet, czas ostatniej aktualizacji i źródło pakietu. Dla konfliktów plików i blokady bazy wykorzystaj procedurę `arch-pacman`, jeśli jest udostępniona. Tutaj rozpatruj specyfikę repozytoriów CachyOS.

Odczytaj tylko potrzebne, dozwolone fragmenty `/etc/pacman.conf`, wskazanych mirrorlist i logu transakcji. Porównaj `pacman -Qi NAZWA` z `pacman -Si NAZWA`, jeśli szablony na to pozwalają. Nie proś o źródła zawierające dane logowania; w takim przypadku potrzebny jest minimalny fragment zredagowany przez użytkownika. Nie odczytuj całego magazynu kluczy. Brak uprawnień oznacza brak danych do diagnozy.

## Decyzje

| Obserwacja | Dalsze sprawdzenie |
|---|---|
| 404 lub niezgodność wersji między mirrorami | Ustal aktualność lokalnej bazy i wybranego mirrora; sprawdź ogłoszenia. Dopiero potwierdzony problem mirrora uzasadnia jego zmianę. |
| Timeout pobierania | Oddziel błąd jednego mirrora od problemu połączenia lub DNS. Polecenie w piaskownicy bez sieci nie jest testem łączności hosta. |
| Niepoprawny podpis | Ustal pakiet, podpisujący klucz, czas systemowy i stan odpowiedniego pakietu keyring. Nie wyłączaj weryfikacji podpisów. |
| `local is newer than` | Sprawdź, czy to opóźniony mirror, celowy downgrade w repozytorium czy pakiet z innego źródła. Samo ostrzeżenie nie uzasadnia obniżenia wersji. |
| Nieoczekiwana architektura lub instrukcja CPU | Sprawdź faktycznie włączone repozytoria i zgodność CPU według aktualnej dokumentacji; nie wnioskuj tylko z nazwy rodziny procesora. |

## Ewentualna zmiana

- Zmiana mirrorlist, aktualizacja keyringu i synchronizacja pakietów to oddzielne plany z zakresem transakcji i zgodą. Zachowaj poprzednią konfigurację, aby móc cofnąć konkretną zmianę.
- Podwójne `-u` dopuszcza downgrade pakietów. Przed takim planem wyjaśnij, dlaczego wersje z repozytorium są właściwe i jakie pakiety mogą się cofnąć.
- Nie podawaj uniwersalnej recepty kasowania keyringu, baz lub ręcznego podpisywania klucza. Przy podejrzeniu naruszenia zaufania zatrzymaj instalację i zweryfikuj aktualną procedurę oraz odcisk klucza w źródle dystrybucji.
- Zmiana poziomu zoptymalizowanych repozytoriów obejmuje wiele pakietów. Wymaga odrębnego zadania, zgodności CPU, planu odzyskania i aktualnej dokumentacji. Nie wykonuj jej jako naprawy pojedynczego pakietu.
- Nie dodawaj nowych uprawnień ani poleceń samodzielnie. Jeśli kontroler nie obsługuje operacji, przedstaw plan do decyzji użytkownika.

## Wynik

Podaj porównane wersje i źródła, potwierdzoną przyczynę lub hipotezę oraz jeden następny krok. Po zatwierdzonej zmianie sprawdź ten sam pakiet i pierwotny błąd; zniknięcie ostrzeżenia nie dowodzi sprawności aplikacji.

## Źródła

- [CachyOS FAQ: błędy pacmana](https://wiki.cachyos.org/cachyos_basic/faq/).
- [Zgodność i migracja zoptymalizowanych repozytoriów](https://wiki.cachyos.org/features/optimized_repos/#migrating-from-x86-64-v3-to-x86-64-v4-or-znver4).
- [pacman: opcja sysupgrade](https://man.archlinux.org/man/pacman.8.en).
