---
name: cachyos-hardware
description: Rozpoznaje sprzęt i powiązanie sterowników w CachyOS oraz kieruje diagnostykę do właściwego obszaru. Użyj przy braku urządzenia, błędzie chwd lub regresji po zmianie sterownika, bez automatycznego strojenia.
---

# Rozpoznanie sprzętu w CachyOS

## Cel i dostęp

Ustal jedno urządzenie lub objaw: brak wykrywania, zły sterownik, błąd po aktualizacji albo wydajność pod określonym obciążeniem. Wykorzystaj ustalenia rozmowy; nie zakładaj producenta CPU, karty ani sposobu instalacji.

Diagnoza to tylko odczyt w granicach `permissions.toml`. Polecenia wykonuj pojedynczo i tylko wtedy, gdy ich dokładne argumenty pasują do `allow`. Oczyszczone środowisko, brak dostępu do logu lub sesji oraz ograniczenia piaskownicy nie świadczą o awarii sprzętu.

## Minimalne rozpoznanie

1. Dla urządzeń PCI użyj dostępnego `lspci -k`, dla USB `lsusb`, dla CPU `lscpu`, a dla wersji jądra `uname -r`. Wybierz odczyty potrzebne do objawu zamiast zbierać cały raport.
2. Powiąż identyfikator urządzenia z używanym sterownikiem. Sam zainstalowany pakiet nie dowodzi, że moduł jest aktywny dla tego urządzenia.
3. Przy problemie po aktualizacji ustal działające i zainstalowane jądro oraz czas błędu. Dostępny dziennik analizuj w tym przedziale. Obecność nowego pakietu bez restartu nie oznacza użycia nowego jądra.
4. Gdy pytanie dotyczy chwd, dozwolone `chwd --list-installed` pokazuje profile zainstalowane, a `chwd --list-all` dostępne. Lista dostępnych profili nie potwierdza ich zastosowania ani zgodności z każdą kartą.

## Wybór dalszej ścieżki

- Brak urządzenia w odpowiednim wykazie: ustal wykrywanie, połączenie i błędy inicjalizacji; nie zaczynaj od strojenia sterownika.
- Urządzenie widoczne, brak właściwego sterownika: sprawdź obsługę modelu, zgodność modułu z jądrem i konkretny błąd ładowania.
- GPU AMD ze sterownikiem amdgpu: skorzystaj z `arch-amdgpu`, jeśli jest dostępny. Inny GPU wymaga dokumentacji właściwego sterownika i modelu.
- Problem modułu po zmianie jądra: dostępny `cachyos-kernel`; dźwięk: `arch-audio`; Bluetooth: `arch-bluetooth`; presja pamięci: `arch-zram`.
- Wydajność lub temperatury: odtwórz obciążenie i odczytaj dostępne `sensors`, profile zasilania i pomiary w czasie. Bez zakresu i punktu odniesienia nie proponuj zmian CPU, planisty dysku ani napięć.

Gdy potrzebny Skill nie jest wybrany, nie udawaj jego wczytania. Podaj zakres dalszej diagnostyki i potrzebne źródło lub obserwację.

## Zmiana i sprawdzenie

Automatyczna konfiguracja chwd, wymiana sterownika, parametr jądra i zapis do `/sys` są zmianami systemu. Krótkotrwałość ustawienia nie czyni go bezpiecznym. Najpierw potwierdź zgodność, zakres skutków, stan wyjściowy i sposób powrotu; potrzebna jest osobna decyzja użytkownika. Przy zmianie sterownika lub rozruchu ustal działającą drogę odzyskania systemu.

Testem jest ponowne wykrycie urządzenia, prawidłowy sterownik i powtórzenie pierwotnego scenariusza. Sama poprawna instalacja pakietu nie potwierdza naprawy.

## Wynik

Podaj urządzenie, sterownik i jądro, potwierdzony etap awarii oraz brakujące dowody. Zakończ jednym następnym krokiem w odpowiednim obszarze, bez pakietu niepowiązanych optymalizacji.

## Źródła

- [CachyOS: rozpoznanie i profile chwd](https://wiki.cachyos.org/features/chwd/chwd/).
