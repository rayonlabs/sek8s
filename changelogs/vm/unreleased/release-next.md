### Changed

- The boot-time RTMR3 phase extends the register **once** instead of once per measured file,
  matching the new `SHA384(0^48 || SHA384(hash-list))` definition. **Published RTMR3
  measurements must be regenerated.**

### Fixed

- Boot-time RTMR3 verification was O(n²) and made the measurement phase unusable once
  `/usr/lib` was measured. Each of the 41,106 files spawned its own `awk` that rescanned the
  whole expected-hashes manifest, so per-file cost grew from 5ms to 20ms over the first 3,600
  files and the phase projected to roughly 67 minutes. The manifest is now read once into
  memory and the hash list streamed past it in a single pass. Both formats carry the 96-char
  hash at a fixed end of the line, so paths containing spaces are matched by offset rather than
  by field splitting.

- The rtmr3-measure initramfs hook now includes `xargs` and `tr`, which `tdx-measure` needs for
  its batched hashing.
