# Slicing rules

`rules.py` looks at each plate's shape and fills in Orca settings the request didn't set. An
explicit override always wins, and `"rules": false` on `/slice` turns them off. The rules that
fired come back in the response as `rules`.

| Rule | When | Sets | Why |
|---|---|---|---|
| Round footprint | every piece's footprint circularity is 0.92 or more (discs, rings, spool adapters) | top, bottom and internal solid patterns: concentric | Lines follow the outline, so there are no short zig-zags ending on the curved edge. Fewer direction changes, a faster print and a cleaner surface. Learned on the 200 mm spool ring (2026-10-25). |
| Round and flat | round, and 10 mm tall or less | sparse infill: concentric | A flat ring carries no load through its infill. Concentric is quick there, and it doesn't cross itself or travel back and forth. |
| Large first layer | any piece 120 mm or more across | first layer 30 mm/s (infill 40), nozzle 225 °C, bed 65 °C | A big first layer is where a plate lets go. Slower and hotter helps it stick edge to edge. 225/65 is what part 1 of the spool ring printed with, against the profile's 215/55. |

Sources: the OrcaSlicer wiki's infill pattern reference
(https://www.orcaslicer.com/wiki/print_settings/strength/strength_settings_patterns_quick_reference)
rates concentric as weak in X-Y, so it's only used where strength doesn't matter (flat round
parts), and grid, cubic and gyroid stay the defaults everywhere else. The rest is Print Lab's
own print history.
