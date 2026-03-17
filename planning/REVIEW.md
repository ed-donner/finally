# Review zmian od ostatniego commit

## Podsumowanie
Zmiany są w 100% dokumentacyjne oraz dodają pliki konfiguracyjne dla wtyczki/agentów (.claude + independent-reviewer). W `PLAN.md` pojawia się sporo nowych deklaracji o zachowaniu backendu i API (np. nowe endpointy, walidacja tickerów, reset portfela, historia cen dla sparkline), ale brak tu zmian w kodzie — ryzyko rozjazdu dokumentacji z implementacją.

## Najważniejsze uwagi
1. **Ryzyko niespójności dokumentacji z kodem**: `planning/PLAN.md` deklaruje nowe endpointy i zachowania (np. `GET /api/prices/history/{ticker}`, `POST /api/portfolio/reset`, walidacja tickerów, usuwanie pozycji przy sprzedaży do zera, rolling cache 5 min). Jeśli backend nie został już do tego dostosowany, dokumentacja jest myląca. Proszę potwierdzić spójność albo cofnąć/oznaczyć te fragmenty jako planowane.
2. **`README.md` ma polecenie `open http://localhost:8000`**: to działa tylko na macOS. Dla Windows/Linux to polecenie nie zadziała (np. `start`/`xdg-open`) albo po prostu „otwórz w przeglądarce”. Warto uogólnić.
3. **Automatyczne wywołanie `codex exec` w hooku Stop**: `independent-reviewer/hooks/hooks.json` uruchamia komendę przy zdarzeniu Stop. To zakłada dostępność `codex` w PATH i może prowadzić do nieoczekiwanych wywołań w różnych środowiskach (np. CI, lokalnie bez CLI). Dobrze byłoby to opisać lub dodać możliwość wyłączenia.
4. **`_CLAUDE_origin.md` jako plik luzem**: jeśli to archiwum starego `CLAUDE.md`, lepiej umieścić w `planning/archive/` lub dodać adnotację, po co jest w repo (albo dodać do `.gitignore`, jeśli ma pozostać lokalny).

## Pytania / niejasności
1. Czy backend jest już zaktualizowany zgodnie z nowymi deklaracjami w `planning/PLAN.md` (reset portfela, endpoint historii cen, walidacja tickerów, rolling cache)? Jeśli nie, czy te zmiany mają być tylko planem na później?
2. Czy hook Stop z `codex exec` ma działać w każdym środowisku (lokalnie/CI), czy tylko jako lokalny workflow?

## Testy
Nie uruchamiano testów (brak zmian w kodzie, tylko dokumentacja i pliki konfiguracyjne).
