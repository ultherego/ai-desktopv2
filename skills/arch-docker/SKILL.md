---
name: arch-docker
description: Diagnozuje niedostępny demon Docker, błędy kontenera, sieci i zajętości danych. Użyj do ustalenia właściwego demona i zakresu awarii przed zmianą konfiguracji lub usuwaniem zasobów.
---

# Diagnostyka Dockera

## Zakres i granica dostępu

Ustal jeden objaw i zakres: demon, konkretny kontener, sieć lub miejsce. Ustal też, czy chodzi o demon systemowy, rootless czy zdalny. Oczyszczone środowisko AI Desktop może wskazywać inny endpoint niż terminal użytkownika; nie zakładaj, że oba klienty widzą ten sam Docker.

Gniazdo Dockera pozwala zmieniać hosta mimo montowania plików tylko do odczytu. Nie proponuj ogólnego szablonu `docker` w `allow`. Korzystaj wyłącznie z konkretnych odczytów już dozwolonych przez właściciela. Nie żądaj pliku poświadczeń `~/.docker/config.json`, pełnego środowiska kontenera ani sekretów aplikacji.

## Rozpoznanie i decyzje

1. Dla potwierdzonego demona systemowego sprawdź dostępny `systemctl status docker.service --no-pager`; dla rootless potrzebna jest właściwa sesja użytkownika. Odmowa lub brak sesji nie dowodzą zatrzymania demona.
2. Dozwolone `docker info` potwierdza odpowiedź wybranego endpointu i rzeczywisty sterownik danych. Nie zakładaj sterownika na podstawie dystrybucji. `Cannot connect` rozdziel na błędny endpoint, uprawnienia do gniazda i niedziałający demon.
3. Demon działa: wybierz jeden kontener z dozwolonego `docker ps -a`. Stan `Exited` wymaga kodu zakończenia i logu; sam w sobie nie dowodzi awarii, np. przy zadaniu jednorazowym. Odczyt logu ogranicz, np. `docker logs --tail 100 NAZWA`, jeśli dokładne argumenty dopuszcza polityka.
4. Demon nie startuje: porównaj jego dziennik z dozwoloną konfiguracją. Opcja ustawiona równocześnie w `daemon.json` i argumentach usługi może wywołać konflikt; wskaż konkretną opcję i błąd przed zmianą.
5. Przy sieci oddziel łączność hosta, kontenera i DNS. Widok sieci piaskownicy AI Desktop nie jest widokiem kontenera ani hosta. Przy zajętości danych użyj dostępnego `docker system df`; bez ustalenia właściciela i znaczenia zasobów zakończ na raporcie.

## Zmiana i sprawdzenie

Nie naprawiaj uprawnień przez otwarcie gniazda dla wszystkich. Dostęp do demona działającego jako root, także przez grupę `docker`, daje uprawnienia na poziomie roota.

Restart demona może przerwać kontenery. Zmiana konfiguracji wymaga zachowania poprzednich bajtów, sprawdzenia składni i kontroli skutków dla usług. `prune`, usuwanie wolumenów, zmiana sterownika lub katalogu danych wymagają osobnego uzgodnienia; nie mają automatycznie bezstratnego cofnięcia.

Testuj pierwotny scenariusz i stan konkretnej aplikacji, nie tylko `active` demona. Nie pobieraj obrazu ani nie twórz kontenera testowego bez osobnej decyzji.

## Wynik

Podaj endpoint i rodzaj demona, warstwę problemu, dowód oraz najmniejszy następny krok. Oddziel plan od wykonanej zmiany. Przy braku uprawnień nie obiecuj diagnozy kontenerów.

## Źródła

- [Demon: endpoint, konfiguracja i sieć](https://docs.docker.com/engine/daemon/troubleshoot/).
- [Uprawnienia grupy `docker`](https://docs.docker.com/engine/install/linux-postinstall/).
