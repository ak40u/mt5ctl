# MetaTrader 5 chart files

A *profile* is a set of open charts. Each profile is a folder in the terminal's
data folder, `MQL5/Profiles/Charts/<profile>/`, holding one `chartNN.chr` file per
chart. The terminal opens the profile named by `ProfileLast` in the `[Charts]`
section of `config/common.ini`, or the one given with `/profile:<name>` on the
command line.

MetaQuotes does not publish the `.chr` format. This page describes how mt5ctl
reads it and which parts it relies on. The parser is deliberately generic: it
understands sections and `key=value` lines, keeps everything else as it is, and
gives a typed view only of the few keys a deploy needs. Every assumption about
undocumented behaviour (encodings, period codes, journal lines) lives in
[`mt5spec.py`](../src/mt5ctl/mt5spec.py), with its source.

## Encoding

The terminal writes chart files as UTF-16LE with a byte-order mark and CRLF line
endings, with no line break after the closing `</chart>` (observed on build
6320). mt5ctl also reads UTF-8 (with or without BOM), BOM-less UTF-16LE and
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
expertmode=1
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
| `period_type`, `period_size` | `<chart>` | the timeframe: `period_size` counts minutes (`period_type=0`), hours (`1`), weeks (`2`) or months (`3`); see the table below |
| `name` | `<expert>` | the expert's display name |
| `path` | `<expert>` | the expert's file, relative to `MQL5`, with backslashes |
| `expertmode` | `<expert>` | the expert's permission flags, kept as a number |
| everything | `<inputs>` inside `<expert>` | the expert's inputs, in order, values exactly as stored |

Timeframe codes observed on build 6320:

| Timeframe | `period_type` | `period_size` |
|---|---|---|
| M30 | 0 | 30 |
| H1 | 1 | 1 |
| H4 | 1 | 4 |
| D1 | 1 | 24 |
| W1 | 2 | 1 |
| MN1 | 3 | 1 |

`period_type` is the unit — minutes (0), hours (1), weeks (2), months (3) — and
a day is 24 hours. The other minute and hour timeframes follow the same scheme.

The `<expert>` block (observed on build 6320, with an expert attached in the
terminal and saved on a normal exit) sits directly in `<chart>`, before the
first `<window>`, and holds `name=`, `path=` (relative to `MQL5`, with
backslashes), `expertmode=` and an `<inputs>` block of `name=value` lines.

Bit 0 of `expertmode` is the expert's own "Allow Algo Trading" checkbox:
unticking it turns `expertmode=1` into `expertmode=0` and changes nothing else.
mt5ctl reads it as `Expert.algo_trading`, and `bundle check` warns about charts
whose expert would load without trading. The other bits have not been observed;
the value is otherwise kept as a number.

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

## The order file

Next to the charts, each profile folder holds `order.wnd`: the chart file names,
one per line, in window order (UTF-16LE with BOM, CRLF after every line). The
terminal also opens chart files the list does not mention, and rewrites the list
when it saves the profile (observed on build 6320). A deploy still keeps it in
step, so it never lists a chart that is gone: names of charts it removes are
dropped, charts it adds are appended, and every other entry keeps its place.

## Numbering and ids

When it saves a profile, the terminal closes gaps in chart numbering: with
`chart05` and `chart06` missing, `chart07.chr` comes back as `chart05.chr`. It
also replaces a chart `id=` that two files share with the highest id plus one.
Both observed on build 6320. The id change is harmless (it is not part of the
fingerprint); the renumbering is why a deploy refuses to leave gaps.
