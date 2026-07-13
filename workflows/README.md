# Workflow diagrams

This folder is the single source of truth for the workflow diagrams embedded
in the module READMEs. Each diagram is a plain, human-editable
[Mermaid](https://mermaid.js.org/syntax/flowchart.html) flowchart in a `.mmd`
file; a wrapper script renders them all to consistently styled SVGs (and,
optionally, 500 DPI PNGs).

## Folder layout

| Path | What it is |
|---|---|
| `*.mmd` | Editable Mermaid sources, one per module workflow. No styling boilerplate inside, just the flowchart. |
| `mermaid-config.json` | The shared look: white boxes, black borders/text/arrows, 18px font, tight padding. |
| `generate_workflows.py` | Wrapper that renders every `.mmd` to `svg/` and rewrites the arrowheads into open barbs. |
| `open-arrowheads.css` | Arrowhead styling injected into PNG exports. |
| `svg/` | Generated SVGs, embedded by the module READMEs. Do not edit by hand. |
| `png/` | Optional 500 DPI PNG exports for slides and papers (created by `--png`). |

## How to update a diagram

1. Edit the `.mmd` file (node text, arrows, layout). It is normal Mermaid
   flowchart syntax, so any Mermaid live editor can preview it.
2. Regenerate:

   ```bash
   python3 workflows/generate_workflows.py              # all diagrams
   python3 workflows/generate_workflows.py streamflow.mmd   # just one
   python3 workflows/generate_workflows.py --png        # also 500 DPI PNGs
   ```

   or simply `make workflows` from the repo root.

3. Done. The module READMEs point at `workflows/svg/<name>.svg`, so the new
   rendering shows up automatically; commit the changed `.mmd` and `.svg`
   together.

Requirements: Python 3 and Node.js >= 18. On the first run `npx` downloads
`@mermaid-js/mermaid-cli` and a headless browser, so it takes a minute;
afterwards it is fast.

## Which diagram belongs to which README

| Source | Embedded in |
|---|---|
| `preprocessing.mmd` | [`src/fimbox/preprocessing/README.md`](../src/fimbox/preprocessing/README.md) |
| `download_data.mmd` | [`src/fimbox/preprocessing/download_data/README.md`](../src/fimbox/preprocessing/download_data/README.md) |
| `huc_test.mmd` | [`src/fimbox/preprocessing/huc_test/README.md`](../src/fimbox/preprocessing/huc_test/README.md) |
| `process_bridgedem.mmd` | [`src/fimbox/preprocessing/process_bridgedem/README.md`](../src/fimbox/preprocessing/process_bridgedem/README.md) |
| `calculate_branch.mmd` | [`src/fimbox/preprocessing/calculate_branch/README.md`](../src/fimbox/preprocessing/calculate_branch/README.md) |
| `calibrate_ratingcurve.mmd` | [`src/fimbox/preprocessing/calibrate_ratingcurve/README.md`](../src/fimbox/preprocessing/calibrate_ratingcurve/README.md) |
| `streamflow.mmd` | [`src/fimbox/streamflow/README.md`](../src/fimbox/streamflow/README.md) |
| `fimgeneration.mmd` | [`src/fimbox/fimgeneration/README.md`](../src/fimbox/fimgeneration/README.md) |

## Adding a new diagram

Create `my_module.mmd` here, run the script, then embed it in the target
README with:

```html
<!-- Diagram source: workflows/my_module.mmd - regenerate with `make workflows` -->
<div align="center">
  <img src="../../../workflows/svg/my_module.svg" alt="My module workflow" />
</div>
```

(adjust the number of `../` to reach the repo root from that README).

## Styling notes

- Keep labels as HTML (`<b>`, `<br/>`, `&middot;`) - the config uses
  `htmlLabels: true`.
- Aim for balanced dimensions: snake long chains into 2-3 columns using
  `flowchart LR` with `direction TB` subgraphs (make a grouping column
  invisible with `style S1 fill:none,stroke:none`).
- The open barb arrowheads are added by the script, not by Mermaid - never
  hand-edit the generated SVGs.
