### Changed

- Offline RTMR3 prediction now runs the same `tdx-measure` script the guest runs, instead of
  reimplementing the walk in Python. The script is bundled with the package, so a `pip install
  chutes-cvm` can still predict a measurement with no checkout, and the guest image is built by
  copying that same file in. Previously this module decided independently which files to measure,
  in what order, and how to hash them, and had drifted from the guest in ways that would have made
  a predicted measurement disagree with the one a VM actually produces. Only the chain fold stays
  in Python, because at boot the hardware does the folding.

### Removed

- Removed the lab-validated host topology matrix (`chutes_cvm/host/support_matrix.py`) and the
  `chutes-cvm host setup --topology-matrix` flag that printed it. The matrix was a hardcoded
  set of (Ubuntu, GPU SKU, GPU count) triples maintained by hand and consulted by nothing —
  its lookup helper had no callers, so it documented support rather than enforcing it, and it
  drifted from reality (B300 was listed by the formatter but absent from the data). Host
  support is now determined by probing the actual host with the chutes-cvm CLI, and the set of
  supported profiles is served by the API, so the static table was a second source of truth
  with no way to stay correct. The validated-topology table in `host-tools/README.md` remains
  as operator documentation.

### Fixed

- Measurement generation now skips a host whose GPUs are not all the same model instead of
  measuring it as whichever model was listed first. Such a host cannot launch anyway —
  launching has always rejected mixed hardware — so the entry it produced was unusable
  rather than wrong in a dangerous way; it is now reported as pending with the reason,
  alongside the other hosts that cannot yet be generated offline.

- Each GPU profile now describes exactly one graphics card. The RTX PRO 6000 profile
  covered both the Workstation and Server editions, so a Workstation host was measured
  using hardware details read from a Server card. Workstation Edition is not supported and
  no longer resolves — such a host is reported as pending rather than measured incorrectly.
  A profile carries reserved CPU counts, guest memory rules, firmware and confidential
  computing settings as well, so two cards that happen to agree on those today could
  quietly diverge later.
