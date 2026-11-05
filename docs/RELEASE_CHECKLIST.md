# Lista kontrolna wydania

Dla opiekunów projektu. Odhaczaj dla konkretnego commita: wynik innego commita niczego nie poświadcza.

## Wersja

- [ ] Numer `pkgver-pkgrel` w `packaging/arch/PKGBUILD` jest nowy.
- [ ] Ten sam numer w README (wiersz o aktualnym pakiecie i nazwa pliku w bloku instalacji), na górze `CHANGELOG.md`,
      w `scripts/accept_web_candidate.fish` i `tests/host_analysis_acceptance.py`.
- [ ] Wersja klienta w README i w `ai_desktop/analysis_contract.py` jest ta sama, a suma kontrolna w README i `PKGBUILD` jest ta sama i pasuje do pobranego pliku.
- [ ] Wpis w `CHANGELOG.md` opisuje zmiany prostym językiem.

## Testy

- [ ] `python3 -B scripts/verify.py --mode ci --fail-fast` przechodzi na tym commicie.
- [ ] GitHub Actions są zielone na tym commicie.
- [ ] `python3 -B scripts/verify.py --mode local --fail-fast` przechodzi na CachyOS lub Arch.
- [ ] Zmiana izolacji, uprawnień, systemu plików, sieci lub logowania została sprawdzona na prawdziwym systemie.

## Pakiet

- [ ] Budowa w czystym checkoutcie: `AI_DESKTOP_PUBLIC_ARTIFACT=1 makepkg --cleanbuild` w `packaging/arch`. Ta zmienna odmawia
      budowy, gdy obok leży plik klienta.
- [ ] Pakiet do rozpowszechniania nie zawiera `claude-code` ani `codex`.
- [ ] Instalacja, aktualizacja i `pacman -R` wykonane według README; lista pozostających danych się zgadza.
- [ ] `ai-desktop doctor`, `ai-desktop status` i jedno rzeczywiste pytanie przez `ai-desktop ask` działają.

## Treść repozytorium

- [ ] Brak sekretów, plików logowania, binarek klienta i danych konkretnego komputera lub osoby.
- [ ] Linki w dokumentacji prowadzą do istniejących plików.
- [ ] `docs/KNOWN_LIMITATIONS.md`, `SECURITY.md` i `docs/THREAT_MODEL.md` opisują bieżące zachowanie.
- [ ] Przed rozpowszechnianiem sprawdzone warunki Claude Code i zasady korzystania z subskrypcji.

## Publikacja

- [ ] Wersja poniżej 1.0 jest oznaczona jako rozwojowa.
- [ ] Znacznik i opis wydania wskazują dokładny commit.
