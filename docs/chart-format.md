# MetaTrader 5 chart files

A *profile* is a set of open charts. Each profile is a folder in the terminal's
data folder, `MQL5/Profiles/Charts/<profile>/`, holding one `chartNN.chr` file per
chart. The terminal opens the profile named by `ProfileLast` in the `[Charts]`
section of `config/common.ini`, or the one given with `/profile:<name>` on the
command line.

MetaQuotes does not publish the `.chr` format. This page describes how mt5ctl
reads it and which parts it relies on. The parser is deliberately generic: it
understands sections and `key=value` lines, keeps everything else as it is, and
gives a typed view only of the few keys a deploy needs.

## Encoding

The terminal writes chart files as UTF-16LE with a byte-order mark and CRLF line
endings. mt5ctl also reads UTF-8 (with or without BOM), BOM-less UTF-16LE and
Windows-1252, and writes a file back in the encoding, BOM, line ending and final
newline it was read with. A file that is parsed and written back unchanged is
byte-identical to the original.

## Structure

```text
<chart>
id=133402917718373291
symbol=EURUSD
description=Euro vs US Dollar
period_type=1
period_size=4
digits=5
...display settings...

<expert>
name=Trend EURUSD H4
path=Experts\Portfolio\Trend EURUSD H4.ex5
expertmode=33
<inputs>
RiskPerTrade=100.0
MagicNumber=1001
</inputs>
</expert>

<window>
height=100.000000
<indicator>
name=Main
...
</indicator>
</window>
</chart>
```

- A line `<tag>` opens a section and `</tag>` closes it; sections nest.
- `key=value` lines are split on the first `=`, so values may contain `=`.
- Blank and unrecognised lines are preserved verbatim.

### What mt5ctl reads

| Key | Where | Meaning |
|---|---|---|
| `symbol` | `<chart>` | the chart's symbol |
| `period_type`, `period_size` | `<chart>` | the timeframe: `period_size` counts in units of `period_type` (0 minutes, 1 hours, 2 days, 3 weeks, 4 months), so `1`/`4` is H4 and `0`/`15` is M15 |
| `name` | `<expert>` | the expert's display name |
| `path` | `<expert>` | the expert's file, relative to `MQL5`, with backslashes |
| `expertmode` | `<expert>` | the expert's permission flags, kept as a number |
| everything | `<inputs>` inside `<expert>` | the expert's inputs, in order, values exactly as stored |

Only the `<expert>` section directly inside `<chart>` is the chart's expert
advisor. Indicators carry their own `<inputs>` inside `<window>` blocks; mt5ctl
never confuses the two.

### Fingerprint

To decide whether a deployed chart has changed, mt5ctl compares a fingerprint
of `symbol`, `period_type`, `period_size` and the full `<expert>` view (name,
path, mode, inputs) instead of the file's bytes. Display state the terminal saves
as you work — scroll position, scale, window geometry — does not count as a
change.

## Editing charts from Python

```python
from mt5ctl.chart import ChartFile, Expert, Timeframe

chart = ChartFile.read("template.chr")       # a chart saved by the terminal
chart.symbol = "GBPUSD"
chart.timeframe = Timeframe.parse("H1")
chart.set_expert(
    Expert(
        name="Breakout GBPUSD H1",
        path=r"Experts\Portfolio\Breakout GBPUSD H1.ex5",
        inputs=(("RiskPerTrade", "100.0"), ("MagicNumber", "1002")),
    )
)
chart.write("bundle/MQL5/Profiles/Charts/Default/chart02.chr")
```

`set_expert` edits an existing `<expert>` block in place, so keys the terminal
wrote there survive; it replaces the inputs wholesale. Values containing a line
break, and keys containing `=` or starting with `<`, are refused because they
would change the file's structure.

Each chart's `id=` should be unique within a profile; when you generate charts
from one template, give each a fresh value.
