# VORTEX MLB Prop Analyzer

This is a local, MLB-only analyzer. It does not connect to the Discord bot or website.

It evaluates a manually supplied player prop using VORTEX data and filters: L5/L10/L20 form, projection edge, probable starter quality, pitcher home/away ERA and FIP, handedness, BvP, pitch arsenal and the batter's results by pitch type, Statcast contact quality, lineup position, opposing bullpen, defense, park, game-time weather, variance, and sample-size confidence. The report starts with a beginner-friendly paragraph and a seven-factor matchup scorecard where positive numbers help the selected Over/Under and negative numbers hurt it.

## Run it

The easiest option is to double-click `run_analyzer.bat`. On its first run it creates a private environment inside the Analyzer folder and installs the four required packages automatically. Later launches start immediately.

To show the complete technical report with every model weight, reliability
score, pitch, split, and diagnostic, double-click `run_analyzer_details.bat`.

From the Vortex folder, you can alternatively set it up manually:

Install the existing project dependencies once (preferably in a virtual environment):

```powershell
python -m pip install -r Analyzer/requirements.txt
```

Then run an analysis:

```powershell
python Analyzer/analyzer.py "Aaron Judge" hits 0.5 over
```

Or launch it without arguments for interactive prompts:

```powershell
python Analyzer/analyzer.py
```

The interactive flow asks for the player, prop, line, and then **Over or Under**. It accepts `O/U` and `More/Less` as shortcuts.

If the probable starter is TBD or you want to evaluate a future matchup:

```powershell
python Analyzer/analyzer.py "Aaron Judge" tb 1.5 over --pitcher "Tarik Skubal"
```

Add `--json` to return the full structured evidence and scorecard for use in another local program.

Supported hitter props: hits, total bases (`tb`), home runs (`hr`), RBIs, runs, batter strikeouts, walks, hits+runs+RBIs (`hrr`), and PrizePicks hitter fantasy score (`fs`).

`Ks` is role-aware: entering a pitcher automatically launches the dedicated pitcher-strikeout model; entering a hitter evaluates batter strikeouts. The pitcher model uses recent K distribution, workload/leash, opponent K% overall and versus the pitcher's hand, command, opponent offense, Savant whiff/putaway by pitch, and confirmed-lineup pitch-type tendencies. If the lineup is not confirmed, lineup-specific arsenal credit is withheld.

The confidence score is 0–100. Missing data is treated as unavailable and reduces confidence; it is never silently treated as a favorable matchup.
