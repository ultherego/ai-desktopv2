---
name: cachyos-scheduler
description: Diagnozuje działanie sched-ext i porównuje zmianę planisty na CachyOS. Użyj przy scx_loader, scxctl, awarii planisty lub pomiarze wpływu planowania na wydajność i energię.
---

# Sched-ext: stan, diagnoza i próba

## Ustal stan

1. Ustal cel i objaw: awaria, opóźnienia czy planowany pomiar. Bez konkretnego problemu nie proponuj strojenia.
2. Odczytaj działające jądro przez `uname -r` oraz wersje dostępnych narzędzi. Nazwa wariantu jądra nie jest wystarczającym dowodem obsługi funkcji.
3. Jeżeli polityka i widok systemu pozwalają, odczytaj `/sys/kernel/sched_ext/state` i `/sys/kernel/sched_ext/root/ops`. `enabled` wraz z nazwą operacji potwierdza aktywność planisty BPF; `disabled` wskazuje brak aktywnego sched-ext. Brak ścieżki lub odmowa odczytu wymaga rozróżnienia obsługi jądra od ograniczenia dostępu.
4. Ustal menedżera i jego stan: `systemctl status scx_loader.service --no-pager`, a przy dostępnym kliencie `scxctl get` i `scxctl list`. To przykłady odczytu, wykonywane wyłącznie po dopasowaniu do polityki.

## Interpretuj wynik

- Brak aktywnego planisty może być zamierzony. Nie oznacza, że system jest zepsuty lub nie używa planisty CPU.
- Aktywna usługa nie dowodzi załadowania planisty. Porównaj stan usługi z wynikiem jądra i logiem z tego samego czasu.
- Błąd połączenia klienta D-Bus w piaskownicy nie dowodzi awarii usługi na hoście. Nie przenoś odczytu do `change` ani nie rozszerzaj dostępu, aby go obejść.
- Gdy planista zniknął po błędzie, przeczytaj dostępny fragment dziennika menedżera i jądra. Kernel może odłączyć planistę BPF po błędzie. Ustal powód z logu zamiast zakładać konflikt konkretnego programu.
- Przy dwóch mechanizmach zarządzania lub samoczynnej zmianie profilu ustal, który steruje stanem i kiedy. Nie wyłączaj usług tylko na podstawie ich obecności.

## Zmiana z pomiarem

Zachowaj bazę: jądro, aktywny planista i tryb, menedżer oraz jego konfiguracja. Wersja `scxctl` decyduje o nazwach i parametrach; wybierz dostępny wariant, bez listy stałych numerów profili lub obietnicy poprawy.

Zaplanuj jedną odwracalną próbę, miarę i identyczne obciążenie dla kilku powtórzeń. Uwzględnij responsywność, stabilność i pobór mocy, gdy są celem. Sterowanie wymaga osobnej zgody i mechanizmu aplikacji, który rzeczywiście ma dostęp; Skill nie nadaje praw administratora ani polkit.

Wycofanie ma przywracać zapamiętany stan. `scxctl restore` przywraca ustawienie domyślne z konfiguracji, które może różnić się od stanu sprzed próby. Przy zawieszeniu lub pogorszeniu zakończ próbę; trwałą konfigurację proponuj dopiero po pomiarze. Nie dodawaj parametrów jądra w ramach zwykłego porównania planistów.

## Wynik

Podaj potwierdzony stan jądra i menedżera, przyczynę lub hipotezę z dowodem, bazę i wynik próby oraz stan wycofania. Przy braku logów lub widoczności zakończ na konkretnej luce, bez diagnozy z samego komunikatu dostępu.

## Źródła

- Kernel, stan i zachowanie sched-ext: <https://www.kernel.org/doc/html/latest/scheduler/sched-ext.html#switching-to-and-from-sched-ext>.
- CachyOS, zarządzanie i porównanie: <https://wiki.cachyos.org/configuration/sched-ext/>.
