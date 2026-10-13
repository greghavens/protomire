# e658e32 acceptance: failed candidate, preserved evidence

This is not a passing release. The complete aggregate exited 1. Its native
integration group had 26 failures; the bounded runner cancelled the lifecycle
group and did not start components/concurrency. Cleanup completed. Source-pin,
frontend, service and reviewed custom replay successes do not override these
failures. Raw first-attempt logs remain in the local evidence archive; their
SHA256 values are recorded in receipt.json.

Corrections address genuine production regressions separately from test-owned
synthetic source/dataset reviews. No public pretrained rights were manufactured.
The public fixture remains blocked pending authoritative license review. Mac
initial full/LoRA execution passed after correcting two harness invocations;
warm-start then correctly refused an unreviewed changed dataset revision.

Reproduce locally with the pinned environments and:
MODELBUILDER_RUN_INTEGRATION=1 MODELBUILDER_NATIVE_TEST_PROCESSES=2 ./scripts/test_all.sh

The later corrective batch offers collect-all diagnostic mode, preserving a
failing exit status, so one blocked group does not conceal remaining results.
