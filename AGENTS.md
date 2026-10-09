# GWProv Agent Working Traits

Every AI agent working in this repository should bring these perspectives to
its changes:

- **CLI and data-processing expert:** Treat terminal output as a user interface.
  Every command should have the same thoughtful design quality as `gwprov ps`:
  concise, scannable, readable at ordinary and narrow terminal widths, and
  explicit about state and next actions. Use Rich for adaptive tables, useful
  color, progress, hierarchy, and paging; use Rich-Argparse for help. Honor
  `NO_COLOR`, avoid raw ANSI codes, and provide a `--no-pager` path for pageable
  commands. Keep machine-readable output (especially JSON) structured, stable,
  and free of human-oriented decoration.
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

Before finishing a CLI change, inspect its actual help/output at a normal
terminal width and a narrow width, and check that redirected output remains
clean and script-safe. Do not accept default `argparse` formatting or a plain
dump of internal structure as finished user-facing help. Help should explain
what commands do, group related operations, show useful examples, and point to
the next level of help. A command map should describe the real parser hierarchy;
any machine-facing representation used by shell completion must remain
deterministic and free of color or layout glyphs.

## Command scope and active device

- Keep three scopes clear in the command tree: GWProv-wide inventory and
  configuration, all-device inventory, and controls for the selected device.
- `gwprov show` is the concise overview; `adapters`, `devices`, `projects`,
  `profile`, `perf`, and `sdcard` are first-class top-level resource commands. Their
  `show CATEGORY` forms remain concise dashboard views. `gwprov ps` remains the
  detailed live-state view. Do not duplicate its technical columns in `devices`.
- Keep SD-card resource operations and SD-image file operations under the single
  `sdcard` command (`add/list/remove/create/compose`). Preserve `sd` as a short
  command alias rather than presenting a second SD concept in help or the tree.
- Register remote `gnwmanager serve` endpoints under `gwprov adapters`; one
  endpoint represents one device. Store per-user adapter configuration in the
  platform config directory and honor `GWPROV_CONFIG_DIR`.
- `gwprov set active DEVICE` stores a per-user device ID. Commands that control
  one device should use this selection when an explicit target is omitted, and
  fail with a direct message if the selection is missing, stale, or the wrong
  device kind. Never silently pick the first adapter or VM.
- `gwprov set profile PROFILE` and `gwprov set sdcard CARD` attach managed
  resources to the active device. `gwprov apply` consumes those assignments:
  it launches the assigned profile for GWemu or deploys it to hardware. A
  registered SD-card path is an already-mounted folder/drive; registration
  never formats media. Applying its profile overlays files and retains extras.
- Shell completion must query local profile, device, and SD-card registries
  through script-friendly output modes. Device completion must not poll QMP or
  open a physical debug session.
- Keep device-interacting commands shallow in the tree and group them under
  selected-device controls. Label remaining directory-driven firmware tasks
  (`retro-go`, `ofw`) as file workflows that are not yet device-scoped; keep
  project-catalog commands in their own group.
- Filesystem edits must make the target and side effect clear. FrogFS edits
  rebuild the image and update its declared size; LittleFS edits operate in
  place; SD edits operate in place. Filesystem creation/formatting must state
  the required size and type and explicitly confirm replacement of existing
  data.
- Keep profile-name workflows distinct from direct image-file workflows, but
  expose them through the same filesystem verbs where their behavior is the
  same. Preserve existing commands as aliases or compatible entry points while
  transitioning help and completion.
