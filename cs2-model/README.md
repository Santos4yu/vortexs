# CS2 Prop Research Model

Standalone CS2 player-prop research project.

This project is intentionally separate from VORTEX's Discord bot, MLB model,
WNBA model, and Krazy Picks website. It will have its own data storage,
configuration, model logic, tests, and interface.

## Initial scope

- Import daily CS2 map matchup documents.
- Import full-page prop-board screenshots.
- Extract and confirm player, team, opponent, market, line, and match time.
- Start with Maps 1-2 kills; add headshots only after the kills model is tested.
- Build projections from player performance, expected maps and rounds, map pool,
  opponent, role, roster status, event quality, and sample quality.
- Save every evaluated prop for chronological backtesting and honest grading.

## Project boundaries

- No Discord commands or bot dependency.
- No shared MLB or WNBA scoring logic.
- No claims of model accuracy before backtesting and live calibration.
- HLTV and Bo3.gg data remain source-labeled and are never silently mixed.
- Missing data lowers confidence; it is never replaced with invented values.

## Planned folders

- `app/` - standalone user interface and application entry point
- `ingestion/` - document, screenshot, and future provider importers
- `model/` - features, projections, simulation, and calibration
- `storage/` - standalone database schema and repositories
- `tests/` - importer and model tests
- `data/` - local imports and generated data (ignored by Git)

## Current build

- Standalone CS2 Prop Lab web interface
- PrizePicks Chrome/Edge browser helper
- Paste-text fallback importer and confirmation table
- Maps 1-2 kills/headshots normalized market schema
- Opportunity-based projection engine with sample, roster, map-coverage and
  source-quality gates
- PandaScore connection and upcoming-match adapters
- Permanent D1 schema for every evaluation and final result
- Honest `NO_DATA` lock when historical evidence is missing or demonstrational

## Required live connection

Set `PANDASCORE_API_KEY` to a PandaScore token whose plan includes historical
Counter-Strike player and match statistics. Fixture-only access can populate
the schedule but cannot unlock player-prop recommendations.

The project does not scrape HLTV. Current HLTV terms prohibit automated
scraping, and a blocked or unauthorized feed is not an acceptable foundation
for a permanent betting record.

## PrizePicks helper

The unpacked extension is in `prizepicks-helper/`. Load that folder through the
Chrome or Edge extensions page, open the PrizePicks CS2 board, scroll through
all cards, and press **Scan visible board**. Every captured line is sent to the
confirmation screen before it can be evaluated.
