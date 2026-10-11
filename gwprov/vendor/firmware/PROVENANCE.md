# Vendored firmware artifact

`gnw_bootloader_0x08032000.bin` is the SD bootloader from
`sylverb/game-and-watch-bootloader`, release `v1.0.8`, asset
`gnw_bootloader_0x08032000.bin`.

- Size: 50,248 bytes
- SHA-256: `43fd718c1a53dda140f29e4025fc8b6d3bfa71f19a710139135f998e4de022d7`
- Linked at `0x08032000`; reset vector is `0x0803A53D`.
- Maximum supported bank-1 image end: `0x08040000`.

The GWProv default uses this pinned artifact for reproducible profile creation.
The accompanying license is from the upstream `game-and-watch-bootloader`
checkout. To update the artifact, replace it with the release asset and revise
its hash, size, and version here.
