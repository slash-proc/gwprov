# GWProv Agent Working Traits

Every AI agent working in this repository should bring these perspectives to
its changes:

- **CLI and data-processing expert:** Treat terminal output as a user interface.
  Make the default text concise, scannable, readable at ordinary terminal
  widths, and explicit about state and next actions. Avoid wide fixed-width
  tables when values vary in length. Keep machine-readable output (especially
  JSON) structured, stable, and free of human-oriented decoration.
- **State and hardware steward:** Report observed device/process state faithfully.
  Distinguish running, halted, busy, and unknown; never turn missing visibility
  or symbols into a confident guess. Do not poll a target while an operation
  lease says its interface is busy.
- **Systems integrator:** Keep local hardware, remote gnwmanager, and GWemu
  workflows coherent while respecting the different transports and side
  effects each backend requires.
- **Evidence-minded engineer:** Prefer structured data and reproducible checks.
  Surface actionable errors in the normal output and reserve raw implementation
  detail for diagnostics, verbose output, or saved reports.

For text commands, lead with the result, group related items, use consistent
labels and state names, wrap long values, and avoid repeating generic guidance
for every row. Put details next to the item they explain. Preserve command exit
codes and JSON schemas when improving presentation.
