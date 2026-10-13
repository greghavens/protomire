# Frozen merged runtime acceptance, 2026-10-13

Candidate `5b38d9602b6f8ae36fc0dcaf28f0a6da8c8f7d4a` pins runtime
`0e825487ef94fa613e1606f458177ad009b1a5cf`, with 114 source/lock files.
Both shipping manifests and the working-tree runtime were rechecked against Git.
This is technical runtime verification, not full-product, installer or GUI acceptance.

- [Linux aggregate](linux-aggregate.json): full script exit 0; 2,058 light passes,
  137 skips, 84 deselections; 357 original frontend passes; 3 offline Modal SDK
  passes. Native groups: 30 lifecycle, 50 integration, 373 components and 18
  concurrency passes. Eight component hardware skips remain explicit. Queue:
  1,923.375 seconds, all groups exit 0, cleanup complete, zero signaled processes.
- [Linux installed-wheel replay](linux-portability.json): custom and pretrained
  fixtures each pass 35/35 comparisons with all 8/12 output hashes equal; 224
  actual portability tests pass, no skips. Four fresh environments/native stores,
  source and installed bytes verified, all four workers terminated.
- [Mac runtime](mac-runtime.json): exact candidate, six required MLX cases and
  one mandatory MPS case; 363 service passes/13 skips; 357 frontend passes.
  Fresh installed wheel and independent 34-package MLX environment reload both
  full/LoRA exports with zero maximum difference across 48 logits each. MLX
  training/export uses Metal; durable live evaluation uses MLX CPU.
- [Source verification](source-verification.json) preserves the exact 114-file
  runtime inventory and shipping-manifest hashes.
- [Original receipt hashes](source-receipt-sha256.json) identify the raw local
  inputs used to derive these summaries. Personal paths, hostnames, logs, model
  weights, source archives and unrelated private reports are excluded.

The full-script exit code was observed by the parent execution session; the
stdout log alone does not encode that code. Test counts derive from JUnit and
terminal logs. Deselections derive from logs, not JUnit. Selections overlap and
are never added as unique coverage. The later uncommitted distribution/frontend
work and its separate test run are not silently substituted for the 357-test
original aggregate or exact Mac candidate.

The Mac handoff archive SHA256 is
`9f72f2c6972606ffceaca9a92dfcc8e2c028dc30417bcf256e90341705a0b852`;
all 107 manifest members matched. Its earlier missing-output-directory and
nonexistent-test-file invocation errors remain unsuccessful attempts. The
corrected runs did not change runtime source. Prior Mac native GUI work stopped
when the machine relocked and does not establish a GUI pass.

Reproduction: [installed-wheel harness](../portability-0e82548/replay-exact-runtime.sh),
[post-run verifier](../portability-0e82548/verify-final-replay.py) and
[installed-wheel probe](../portability-0e82548/probe-installed-wheel.py).
The harness requires new output directories, the locked dependency cache,
immutable pretrained resources and declared local system prerequisites. It does
not regenerate the original structured installation inventory; fresh install,
dependency-check logs and runtime inventories support this run.

Remaining gates include a verified single-app installer and managed MCP startup,
current native GUI acceptance, actual CUDA, other-platform release acceptance,
live authenticated vendor assistants, online tracking, production remote
execution/billing controls, hostile-code isolation and redistribution licensing.
No hosted CI or paid cloud execution was used for these receipts.
