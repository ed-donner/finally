# FinAlly — AI Trading Workstation

FinAlly (Finance Ally) to stacja tradingowa z asystentem AI, inspirowana terminalem Bloomberga. Streamuje dane rynkowe w czasie rzeczywistym, umożliwia handel symulowanym portfelem i integruje czat LLM, który analizuje pozycje i wykonuje transakcje w imieniu użytkownika.

Projekt zbudowany w całości przez agenty kodujące (Coding Agents) jako projekt końcowy kursu agentic AI.

## Stos technologiczny

- **Frontend**: Next.js + TypeScript (static export), Tailwind CSS, Recharts
- **Backend**: FastAPI (Python), uv
- **Baza danych**: SQLite
- **Dane rynkowe**: symulator GBM (domyślnie) lub Massive API (Polygon.io)
- **AI**: LiteLLM → OpenRouter (Cerebras) ze structured outputs
- **Infrastruktura**: pojedynczy kontener Docker, port 8000

## Szybki start

```bash
# Skopiuj i uzupełnij zmienne środowiskowe
cp .env.example .env
# Ustaw OPENROUTER_API_KEY w pliku .env

# Uruchom aplikację
docker compose up -d --build

# Otwórz w przeglądarce
open http://localhost:8000
```

## Zmienne środowiskowe

| Zmienna | Wymagana | Opis |
|---------|----------|------|
| `OPENROUTER_API_KEY` | Tak | Klucz API OpenRouter dla czatu AI |
| `MASSIVE_API_KEY` | Nie | Klucz Massive API — bez niego działa symulator |
| `LLM_MOCK` | Nie | `true` = deterministyczne odpowiedzi LLM (testy) |

## Funkcjonalności

- Watchlist z 10 domyślnymi tickerami i cenami aktualizowanymi w czasie rzeczywistym (SSE)
- Sparkline mini-wykresy i szczegółowy wykres wybranego tickera
- Handel — zlecenia rynkowe, natychmiastowa realizacja, portfel startowy $10 000
- Heatmapa portfela (treemap) kolorowana wg P&L
- Tabela pozycji z niezrealizowanym zyskiem/stratą
- Asystent AI — analiza portfela, wykonywanie transakcji i zarządzanie watchlistą przez czat
- Reset portfela do stanu początkowego

## Struktura projektu

```
finally/
├── frontend/          # Next.js (static export)
├── backend/           # FastAPI (uv project)
├── planning/          # Dokumentacja projektowa
├── test/              # Testy E2E (Playwright)
├── db/                # SQLite (volume mount)
├── Dockerfile
└── docker-compose.yml
```

## Zarządzanie

```bash
docker compose up -d --build   # Start
docker compose down             # Stop (dane zachowane)
docker compose down -v          # Reset (usuwa bazę danych)
```

## Licencja

Patrz [LICENSE](LICENSE).
