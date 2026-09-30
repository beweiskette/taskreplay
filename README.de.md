# taskreplay

[English version](README.md)

Coding-Agenten an den eigenen, bereits erledigten Aufgaben messen statt an öffentlichen Benchmarks.

SWE-bench, Terminal-Bench und ähnliche Ranglisten messen, wie Agenten mit fremden Repositories zurechtkommen. Sie sagen nicht, welcher Agent die Fehlerbehebungen in deinem eigenen Code am besten erledigt, wie lange er dafür braucht und was eine gelöste Aufgabe kostet. taskreplay beantwortet das aus deiner Git-Geschichte. Es nimmt abgeschlossene Aufgaben (den Commit davor, eine Aufgabenbeschreibung und einen Testbefehl, der vorher fehlschlug und nachher durchläuft), spielt jede Aufgabe mit mehreren Agenten in getrennten Git-Worktrees nach und hält pro Lauf fest:

- ob die Tests durchlaufen
- Laufzeit
- Tokens hinein und heraus
- Kosten in Geld, wenn der Agent sie meldet oder du Preise hinterlegst
- Anzahl Schritte
- Grösse des Diffs (Dateien, hinzugefügte und entfernte Zeilen)

Der Bericht zeigt pro Agent die Erfolgsquote, die mittlere Laufzeit, die mittlere Tokenzahl und die Kosten pro gelöster Aufgabe, aufgeschlüsselt nach Stichwörtern wie `bugfix` oder `feature`. Es gibt Runner für Claude Code, die Codex CLI, jeden OpenAI-kompatiblen Endpunkt (eine kleine eingebaute Agentenschleife, damit auch günstige Modelle bei Anbietern oder lokale Modelle in LM Studio oder Ollama mitmachen können) und für jeden anderen Agenten, der sich mit einem Shell-Befehl starten lässt.

**Echte Läufe kosten Geld oder Abo-Kontingent.** Jeder Lauf der Runner `claude`, `codex` oder `openai-compatible` ist eine vollständige Agentensitzung. Drei Aufgaben mal drei Agenten mal zwei Versuche ergeben achtzehn Sitzungen. Nimm zuerst `--dry-run` und begrenze echte Läufe mit `--max-cost` und `--max-tasks`.

## Installation

Voraussetzung sind Python 3.11 oder neuer und Git.

```
git clone https://github.com/beweiskette/taskreplay.git
cd taskreplay
python -m venv .venv
.venv/bin/python -m pip install -e .        # Windows: .venv\Scripts\python -m pip install -e .
```

Die einzige Laufzeitabhängigkeit ist PyYAML. Die Agenten-CLIs, die du vergleichen willst (`claude`, `codex` usw.), installierst du separat und meldest dich dort an.

## Schnellstart ohne Kosten

`examples/demo.py` baut ein winziges, erfundenes Repository mit einer Fehlerbehebung in der Geschichte und eine Aufgabendatei mit zwei geskripteten «Agenten»: Der eine schreibt die bekannte Korrektur, der andere tut nichts. Es wird keine API aufgerufen. Der Testbefehl der Demo braucht pytest, installiere es also in dieselbe Umgebung (`pip install pytest`).

```
python examples/demo.py demo
taskreplay validate demo/tasks.yaml
taskreplay run demo/tasks.yaml --runners scripted-fixer,do-nothing --attempts 2 --out demo/results
taskreplay report demo/results
```

Ausgabe der letzten beiden Befehle (Pfade gekürzt):

```
running 4 run(s). Agent CLIs and API runners use real quota or money. No --max-cost set.
[1/4] slugify-punctuation with scripted-fixer (attempt 1)
    PASS  status=ok  0.1s  tokens=4200/350  $0.0179
[2/4] slugify-punctuation with do-nothing (attempt 1)
    FAIL  status=ok  0.1s  tokens n/a  cost n/a
...

runner          runs  pass rate   median time  median tokens  median turns  median diff  total cost  cost/solved  errors  timeouts
--------------  ----  ----------  -----------  -------------  ------------  -----------  ----------  -----------  ------  --------
scripted-fixer  2     100% (2/2)  0.1s         4.5k           3             6            $0.0357     $0.0179      0       0
do-nothing      2     0% (0/2)    0.1s         -              -             0            -           -            0       0

Passed runs by tag:
tag     scripted-fixer  do-nothing
------  --------------  ----------
bugfix  2/2             0/2

Best runner per tag (pass rate, then cost per solved task, then time):
  bugfix: scripted-fixer

HTML report: demo/results/report.html
```

Der HTML-Bericht ist eine einzige, in sich geschlossene Datei (keine externen Skripte, Schriften oder Stylesheets). Er enthält dieselbe Übersicht, eine Tabelle nach Stichwort, eine nach Aufgabe und eine Liste aller Läufe.

## Aufgaben beschaffen

### Aus der Geschichte gewinnen

```
taskreplay mine pfad/zum/repo --since 2026-01-01 --test-cmd "python -m pytest -q {test_files}" --out tasks.yaml
```

`mine` sucht Commits ohne Merge, die sowohl Quelldateien als auch Testdateien ändern. Für jeden Kandidaten legt es zwei Worktrees an und führt den Testbefehl aus:

1. beim Eltern-Commit, mit den Testdateien des Commits: Die Tests müssen fehlschlagen.
2. beim Commit selbst: Die Tests müssen durchlaufen.

Nur Kandidaten, die beide Prüfungen bestehen, landen in `tasks.yaml`. `{test_files}` wird durch die Testdateien ersetzt, die der Commit geändert hat. Der Prompt ist die Commit-Nachricht ohne Zusatzzeilen wie `Signed-off-by`. **Überarbeite ihn**, damit er wie ein Auftrag an einen Agenten klingt. Commit-Nachrichten sind oft zu knapp («fix bug») oder verraten die Lösung. Präfixe nach Conventional Commits werden zu Stichwörtern (`fix:` wird `bugfix`, `feat:` wird `feature`, dazu `refactor` und `performance`).

`taskreplay mine REPO --list` listet nur die Kandidaten auf und führt keine Tests aus. `--limit`, `--max-scan` und `--ref` grenzen die Suche ein.

### Von Hand schreiben

```yaml
tasks:
  - id: parser-empty-input          # eindeutige Kennung
    repo: ../my-project             # relativ zu dieser Datei oder absolut
    base: 3f2c9a1                   # Commit, bei dem der Agent beginnt
    solution: 8be0d44               # optional: die echte Korrektur, für `validate`
    prompt: |
      parse_config("") raises IndexError. It should return an empty Config.
    test: python -m pytest -q tests/test_parser.py
    allowed_files: ["src/parser/*.py"]   # optional, Glob-Muster
    hidden_tests:                   # optional: werden nach dem Agenten eingesetzt
      - path: tests/test_parser.py
        from_commit: 8be0d44        # Datei aus diesem Commit nehmen
      - path: tests/test_extra.py
        from_file: hidden/test_extra.py   # oder lokale Datei kopieren (relativ zu dieser Datei)
    timeout: 900                    # Sekunden für den Agenten (Standard 900)
    test_timeout: 300               # Sekunden für den Testbefehl (Standard 300)
    tags: [bugfix, parser]
```

`taskreplay validate tasks.yaml` prüft jede Aufgabe: Der Testbefehl muss bei `base` fehlschlagen (nachdem die versteckten Tests eingesetzt sind) und bei `solution` durchlaufen, sofern angegeben. Eine Aufgabe, deren Tests schon bei `base` durchlaufen, misst nichts.

Versteckte Tests werden eingesetzt, wenn der Agent fertig ist, und vor der Bewertung. Sie überschreiben alles, was der Agent an diesen Dateien geändert hat. Ein Agent kann also nicht bestehen, indem er den Test löscht oder abschwächt. Mit `allowed_files` nennt der Prompt die erlaubten Muster, und ein Lauf, der andere Dateien ändert, gilt als gescheitert, auch wenn die Tests durchlaufen.

## Runner

`--runners` nimmt Namen entgegen. `claude` und `codex` funktionieren ohne Konfiguration. Alles andere steht unter `runners:` in der Aufgabendatei oder in einer eigenen Datei, die du mit `--config` übergibst:

```yaml
runners:
  claude:                          # Claude Code ohne Zusätze
    type: claude
  claude-sonnet:
    type: claude
    model: sonnet
    allowed_tools: ["Bash(python -m pytest:*)"]   # darf die Tests ausführen
  codex:
    type: codex
    price_per_mtok_in: 1.25        # Beispielpreise: codex meldet keine Kosten,
    price_per_mtok_out: 10.0       # taskreplay schätzt sie aus den Tokens
  cheap-api:
    type: openai-compatible
    base_url: https://api.example.com/v1
    model: some-small-model
    api_key_env: EXAMPLE_API_KEY   # NAME der Umgebungsvariable, nie der Schlüssel selbst
    price_per_mtok_in: 0.3
    price_per_mtok_out: 1.2
  local:
    type: openai-compatible
    base_url: http://localhost:1234/v1   # LM Studio; Ollama: http://localhost:11434/v1
    model: qwen2.5-coder-7b-instruct
  other-agent:
    type: command
    command: "other-agent --yes --message-file {prompt_file}"
```

| Typ | was läuft | Verbrauchsdaten |
|---|---|---|
| `claude` | `claude -p --output-format json --permission-mode acceptEdits --no-session-persistence`, Prompt über stdin, im Worktree | Tokens, `total_cost_usd`, `num_turns` aus dem JSON-Ergebnis |
| `codex` | `codex exec --json --sandbox workspace-write --cd <worktree> --ephemeral -`, Prompt über stdin | Tokens aus den Ereignissen `turn.completed`; Schritte = abgeschlossene Befehle, Dateiänderungen und Nachrichten |
| `openai-compatible` | eingebaute Schleife mit den Werkzeugen `read_file`, `write_file`, `run_command` | Tokens aus `usage`; Kosten aus `usage.cost`, falls der Anbieter sie mitschickt, sonst aus den hinterlegten Preisen |
| `command` | beliebiger Shell-Befehl im Worktree | optionale JSON-Datei `{usage_file}` mit `tokens_in`, `tokens_out`, `cost_usd`, `turns`, `model` |
| `module:Class` | eigene Unterklasse von `taskreplay.runners.base.Runner` | was sie zurückgibt |

Die Berechtigungen sind eng gefasst. Der Claude-Runner nutzt `acceptEdits`: Dateiänderungen im Worktree gehen durch, Shell-Befehle werden abgelehnt, ausser du erlaubst sie in `allowed_tools`. Der Codex-Runner nutzt die Sandbox `workspace-write`. Keiner der beiden setzt die Umgehungsschalter. Mit `permission_mode`, `sandbox` oder `extra_args` lässt sich das ändern, wenn du weisst, was du tust.

`price_per_mtok_in` und `price_per_mtok_out` (USD pro Million Tokens) gibt es bei jedem Runner. Sie greifen nur, wenn der Agent selbst keine Kosten meldet. Die Claude CLI meldet `total_cost_usd` auch bei einem Abo; dann ist das ein rechnerischer API-Preis und kein Betrag, der dir belastet wird.

Platzhalter für den Command-Runner: `{prompt_file}`, `{workdir}`, `{usage_file}`, `{timeout}`, `{task_id}`, `{attempt}`. Die Werte werden für die Shell der Plattform in Anführungszeichen gesetzt. Für wörtliche geschweifte Klammern schreibst du `{{` und `}}`. Dieselben Werte stehen als `TASKREPLAY_PROMPT_FILE`, `TASKREPLAY_WORKDIR`, `TASKREPLAY_USAGE_FILE`, `TASKREPLAY_TASK_ID` und `TASKREPLAY_ATTEMPT` in der Umgebung. Optionen: `stdin: true` gibt den Prompt zusätzlich über stdin weiter, `env:` setzt weitere Variablen.

Optionen der eingebauten Schleife: `max_steps` (Standard 40), `command_timeout` (60 s), `allow_commands` (true), `request_timeout` (180 s), `temperature`, `max_output_chars` (20'000), `system_prompt`.

## Ausführen

```
taskreplay run tasks.yaml --runners claude,codex,cheap-api --attempts 2 --dry-run
taskreplay run tasks.yaml --runners claude,codex,cheap-api --attempts 2 --max-cost 5 --max-tasks 10
```

Für jede Kombination aus Aufgabe, Runner und Versuch geht taskreplay so vor:

1. Es legt einen losgelösten Worktree von `base` in einem neuen temporären Verzeichnis an.
2. Es startet den Runner dort mit dem Zeitlimit der Aufgabe.
3. Es nimmt alle Änderungen in den eigenen Index des Worktrees auf und hält den Diff gegenüber `base` fest.
4. Es setzt die versteckten Tests ein und führt den Testbefehl aus.
5. Es schreibt eine JSON-Zeile nach `results/run-<zeitstempel>.jsonl`, die Ausgabe des Agenten nach `results/logs/` und den Diff nach `results/patches/`.
6. Es entfernt den Worktree (mit `--keep` bleibt er stehen).

Die Läufe gehen Aufgabe für Aufgabe vor, alle Runner an einer Aufgabe, bevor die nächste drankommt. Stoppt das Budget, haben die Runner also ähnlich viele Aufgaben gesehen. `--max-cost` startet keine neuen Läufe mehr, sobald die gemeldeten oder geschätzten Kosten die Grenze erreichen. Der Claude-Runner bekommt zusätzlich `--max-budget-usd` mit dem Restbudget, und die eingebaute Schleife hält selbst an, wenn sie es überschreitet. Läufe ohne Kostenangabe zählen nicht zur Grenze; taskreplay gibt aus, wie viele es waren. `--only id1,id2` wählt Aufgaben aus.

Dein Checkout wird nie verändert. `git worktree add` legt einen kleinen Eintrag unter `.git/worktrees/` an, der mit dem Worktree wieder verschwindet.

## Berichte

```
taskreplay report                 # liest results/*.jsonl, schreibt results/report.html
taskreplay report results/run-20260930-101500.jsonl --html out.html --runners claude,codex
```

Spalten: Läufe, Erfolgsquote, mittlere Laufzeit, mittlere Tokens (hinein plus heraus), mittlere Schritte, mittlere Diff-Zeilen, Gesamtkosten, Kosten pro gelöster Aufgabe (Gesamtkosten geteilt durch bestandene Läufe), Fehler, Zeitüberschreitungen. «Best runner per tag» wählt die höchste Erfolgsquote, bei Gleichstand die tieferen Kosten pro gelöster Aufgabe, dann die kürzere mittlere Laufzeit. Bei wenigen Aufgaben pro Stichwort ist das nur ein Hinweis.

## Einbindung in Claude Code oder Codex

taskreplay ist ein Kommandozeilenwerkzeug, beide Agenten können es also aufrufen. Bewährte Muster:

- Den Agenten die Aufgabendatei entwerfen lassen: Er führt `taskreplay mine . --since "3 months ago" --out tasks.yaml` aus, formuliert die Prompts in der erzeugten Datei um und prüft sie mit `taskreplay validate tasks.yaml`.
- Ein Slash-Befehl für Claude Code, zum Beispiel `.claude/commands/replay.md`:

  ```
  Run `taskreplay run tasks.yaml --runners $ARGUMENTS --dry-run`, show me the plan and the
  number of runs, and only start the real run after I confirm. Then run `taskreplay report`
  and summarise which runner to use for which tag.
  ```

- Für Codex gehören dieselben Anweisungen in `AGENTS.md`.

Läuft taskreplay selbst in einer Claude-Code-Sitzung, entfernt es die Variable `CLAUDECODE` aus der Umgebung des `claude`-Kindprozesses, damit dieser sich nicht als verschachtelt betrachtet. Mit einer echten Sitzung ist das nicht getestet. Beide Sitzungen verbrauchen dein Kontingent.

## Grenzen

- Die Agenten im Worktree sehen deine normale Agenteneinrichtung: Der Claude-Runner lädt deine Einstellungen, `CLAUDE.md`-Dateien, Hooks und Plugins, Codex lädt `~/.codex/config.toml`. Für die Frage «wie schlägt sich meine Einrichtung» ist das realistisch, die Ergebnisse unterscheiden sich aber von Rechner zu Rechner.
- Worktrees sind keine Sandbox. Sie teilen Objektdatenbank und Refs mit deinem Repository. Ein Agent, der im Worktree `git branch -D` oder `git push` ausführt, wirkt also auf dein Repository. Das Werkzeug `run_command` der eingebauten Schleife startet im Worktree, erreicht aber alles, was dein Benutzerkonto erreicht. Nicht vertrauenswürdige Modelle gehören in einen Container oder eine VM.
- Der Testbefehl läuft in deiner aktuellen Umgebung. Ist dein Projekt im Editiermodus aus dem Haupt-Checkout installiert, importieren die Tests im Worktree womöglich den Code des Haupt-Checkouts. Nimm einen Befehl, der aus dem Arbeitsverzeichnis importiert (`python -m pytest` bei flachem oder `src`-Aufbau mit passender pytest-Konfiguration), oder richte im Testbefehl eine eigene Umgebung pro Worktree ein.
- Das Ereignisformat von Codex (`turn.completed` mit Verbrauch, `item.completed` mit Einträgen) ist nach der dokumentierten Ausgabe von `codex exec --json` umgesetzt und an aufgezeichneten Beispielen getestet, nicht an einer echten Codex-Sitzung. Der Claude-Runner ist ebenso mit aufgezeichnetem JSON und einem Ersatzprogramm getestet. Während der Entwicklung wurde kein kostenpflichtiger Agent und keine kostenpflichtige API aufgerufen; von beiden CLIs wurde nur `--help` gelesen.
- Die Schrittzahlen sind zwischen Runnern nicht vergleichbar: Claude meldet Modellschritte, der Codex-Runner zählt abgeschlossene Einträge, die eingebaute Schleife zählt API-Aufrufe.
- Die Kosten sind nur so gut wie ihre Quelle. Claudes gemeldete Kosten sind ein API-Listenpreis, Schätzungen hängen von deinen Preisen ab, und zwischengespeicherte Tokens zählen zum normalen Eingabepreis.
- `mine` erkennt Testdateien an Name und Verzeichnis (etwa `test_*.py`, `*_test.go`, `*.spec.ts`, `tests/`). Ungewöhnliche Strukturen brauchen von Hand geschriebene Aufgaben.
- Prompts aus `mine` stammen aus Commit-Nachrichten und müssen meist überarbeitet werden.
- Wenige Aufgaben und einzelne Versuche ergeben verrauschte Zahlen. Agenten arbeiten nicht deterministisch; für Entscheidungen lohnt sich `--attempts` 2 oder mehr.

## Entwicklung

```
python -m venv .venv
.venv/bin/python -m pip install -e ".[test]"
.venv/bin/python -m pytest -q
```

Alle Tests nutzen Ersatz-Runner, lokale Skripte und ein erfundenes Git-Repository in einem temporären Verzeichnis. Die OpenAI-kompatible Schleife wird gegen einen lokalen Ersatz-HTTP-Server getestet.

## Lizenz

MIT, siehe [LICENSE](LICENSE).
