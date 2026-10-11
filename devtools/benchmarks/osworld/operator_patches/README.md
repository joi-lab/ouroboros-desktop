# Operator patches for the external OSWorld checkout

OSWorld (`xlang-ai/OSWorld-V2`) is a THIRD-PARTY checkout obtained separately and
nothing here is vendored. We never fork it and never commit into it: host-side
adjustments live in this directory as unified diffs, applied by the operator on
top of a clean checkout at the pinned commit (`ALIGNED_UPSTREAM` in
`../run_step_agent.py`; currently
`c261cb57a699bd18db128787ca4e71b749141762`).

**Never patch the benchmark's tasks, evaluators/getters, `evaluate()`, or the
`show_result.py` scoring layout.** Patches here may only touch host/provider
plumbing. Anything that could change a score belongs nowhere.

Apply from the OSWorld checkout root:

```bash
cd /path/to/OSWorld
patch -p0 < /path/to/repo/devtools/benchmarks/osworld/operator_patches/<file>
```

## Patches (v6.76.0)

1. `osworld_docker_lock_timeout.v6760.patch` — raise the docker provider's
   `LOCK_TIMEOUT` from 10s to 60s
   (`desktop_env/providers/docker/provider.py`). That constant bounds the wait
   for ONE global lockfile (`/tmp/docker_port_allocation.lck`) that
   `DockerProvider.start_emulator` holds across BOTH port allocation and
   `containers.run(...)`. A cold `happysixd/osworld-docker` start (image load,
   qcow2 mount, KVM setup) regularly needs longer than 10s, so with several
   lanes booting VMs concurrently every lane but one raises `filelock.Timeout`
   inside `DesktopEnv.__init__` — before its agent ever starts. That is an infra
   zero which looks exactly like a capability zero in the results catalog.
   Only the constant changes; the locking design is upstream's.

   **This patch is HALF the fix, and deliberately so** (owner decision: do
   both). The other half lives on our side and needs no patch:
   `run_step_agent.construct_desktop_env()` retries the `DesktopEnv`
   *constructor*, so a lane that still loses the lock race (or hits any other
   transient boot failure) retries instead of burning the task — that retry is
   the benefit being claimed. Each failed attempt is also torn down, because a
   raise inside `__init__` discards the half-built object and would leave
   whatever `_start_emulator()` started unreachable; treat that as a precaution,
   not as a fix for observed container debris (none was measured). Do not treat
   the patch as sufficient on its own: an unpatched checkout still runs, just
   with more lock-loss retries.

## Not patched (recorded so it is not rediscovered)

- The `vmware` provider has no such lock and needs no patch.
- Nothing in this directory changes observation modality, the action space, the
  step budget, or the proxy policy — those are scaffold decisions disclosed in
  `../METHODOLOGY.md`, not upstream edits.
