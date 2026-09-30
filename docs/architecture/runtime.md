# Runtime extraction and test profile

Requirements: `SPEC_Coolify-Podman.md` §§7–12, 20–23. The [system diagram](system.mmd) was recovered verbatim from spec §8 because its standalone companion was absent.

## Delivered seam (FND-002, incomplete)

`StopApplicationOneServer` resolves `App\Contracts\RuntimeDriver` through constructor injection. `DockerRuntimeDriver::stopApplication` owns Docker discovery, base-deployment selection, graceful stop and idempotent removal. It reuses the inherited Docker helpers, including stored-engine-version timeout selection and non-root sudo adaptation. The existing destination control continues to enforce application deployment authorization and current-team server scope.

Only this operation is extracted. The contract deliberately lists implemented behavior rather than placeholder methods. It is an internal compatibility interface, not a remote API, an operational-agent tool, or the NODE-002 security boundary. Other lifecycle paths still contain Docker commands. No Podman capability or alternate lifecycle owner is selected by this binding. NODE-001/002 must introduce identity-specific selection and restricted execution before native mutations are enabled.

The extraction preserves the action's existing nullable error result and Swarm/unavailable-server guards. The destination component now displays a returned failure and retains its additional-server relationship when stopping fails. Regression tests cover both stop and remove-server paths. No rollback of application or database data is implied by stopping/removing a container.

## Disposable fixture

`scripts/runtime-test-vm` boots a dedicated Debian cloud image from the checksum-pinned `.dev/runtime-vm.json` using QEMU TCG. It does not use libvirt, change host networks, or attach existing disks. Requires Python 3.11+, QEMU, genisoimage, curl, OpenSSH and outbound access to Debian package repositories. Root/operator SSH keys are test-only; the workload user is distinct and has no sudo grant. The installed package versions must be recorded after provisioning; apt packages are not yet a release support matrix.

Commands:

```sh
scripts/runtime-test-vm start
scripts/runtime-test-vm status
scripts/runtime-test-vm ssh cloud-init status --wait
scripts/runtime-test-vm ssh docker version
COOLIFY_RUNTIME_VM_TEST=1 php artisan test --compact tests/Feature/RuntimeDriverDockerIntegrationTest.php
scripts/runtime-test-vm stop
```

State, keys, disk and serial logs live in ignored `storage/app/runtime-test-vm/`. `state.json` persists the provisioning operation ID before mutation. A repeated start verifies that ID through QMP instead of launching another VM. PID-only liveness is insufficient across sandbox PID namespaces; denied or uncertain observations stop the action without preparing another launch. An interrupted launch requires reconciling its PID/socket before retrying. Stop powers off only this fixture via its QMP socket; it retains the disk. Do not use these development credentials or bootstrap privileges as a production node protocol.

QEMU 11.1.1 was provisioned locally. Official [QEMU invocation documentation](https://www.qemu.org/docs/master/system/invocation.html) documents TCG and user-network host forwarding. That documentation is not evidence of a passing workload reboot test. The Debian image is revision 20260914-2601, verified against the official SHA512SUMS retrieved 2026-09-28.

## Evidence boundaries

Laravel 12 interface binding and process-test APIs were checked using installed Boost 2.4.8 `SearchDocs`; implementation dependencies remain pinned by `composer.lock`. [Laravel's container documentation](https://laravel.com/docs/12.x/container) describes binding; it does not establish runtime correctness.

Focused mocked-process and authorization tests pass locally (see `PLAN.md`). Real Docker stop/remove/preserved-preview/idempotence tests also passed using both root and non-root SSH against Docker 26.1.5 on the booted VM. The fixture's initial operator creation needed an explicit primary group because Debian already defines that group; the failed user step was repaired without reinstalling packages, and the harness was corrected. A second fresh fixture completed cloud-init without repair after this correction; its runtime gate rerun is recorded in PLAN.md. The bounded rootless Quadlet/Caddy prototype has passed unchanged reload, failed activation compensation, loopback backend and admin-socket isolation, and reboot persistence. It uses Debian Caddy 2.6.2 with HTTP only, not the production adapters or authentication boundary. The executable gate is `COOLIFY_RUNTIME_VM_TEST=1 python3 tests/Scripts/runtime_prototypes_test.py` after installing the exact Caddy package declared in CI. The runner journals phases and refuses a blind retry after interruption. HTTPS, gateway authentication and the production publication/recovery protocol remain separate gates. Host-local schedules, restricted execution, Compose-provider support and Quadlet are not implemented by this seam.

## Capability observations (NODE-001, incomplete)

`ProbeRuntimeCapabilities` runs the fixed `scripts/node-capability-probe.py` as the configured SSH user with sudo disabled. The probe selects the local Podman connection and the user's manager; it does not install packages, units or workloads. Python 3 is required. Each observation stores UID/GID, manager, storage, installed versions, capability findings, remediation and fingerprints in `runtime_contexts`. Identity includes the server connection and storage; it is not a host-level Podman boolean. Changing SSH identity hides the old observations, and a connection or membership change during a probe prevents publication.

`GET /api/v1/servers/{uuid}/runtime-contexts` and `POST /api/v1/servers/{uuid}/runtime-contexts/probe` require current server-team administration plus the corresponding token ability. The server settings panel uses the same action and authorization boundary. Failed transport probes preserve the last observation and report remediation. Observations have timestamps; they are not standing execution authorization. Generator presence does not prove generator acceptance, and no deployment/profile support is enabled by these findings. NODE-002 must independently enforce authority and reconcile fresh state before native mutations.

## Restricted executor prototype (NODE-002, incomplete)

`scripts/coolify-node-executor.py` accepts one bounded JSON operation through a dedicated OpenSSH forced-command key. The fixed protocol command is `coolify-node-v1`; `restrict` disables forwarding/PTY and isolated Python prevents user Python-path injection. Root-owned policy resolves the principal's resources/actions, epoch, policy version and approved source/effective systemd hashes. Requests contain no unit, filesystem path or shell command. The declared profile is a trusted operator with a dedicated rootless identity, not hostile tenant isolation.

The executor validates generation and deadline, journals intent with SQLite FULL/WAL, serializes node operations, and writes durable outcomes/outbox records. Re-delivery uses the same immutable operation/idempotency IDs. Interrupted effects are observed without reissuing the lifecycle command; ambiguous outcomes block later mutations while permitting observation. Missing journal metadata requires trusted recovery. A restart recovered by observing a new systemd invocation is evidence of convergence, not proof of a causal CLI response.

Real disposable SSH tests cover lifecycle, duplicate outcomes, scope/generation/epoch rejection, command injection, root-login and forwarding denial, and SIGKILL after activation began. The missing-journal guard passed a real hide/restore test without resetting generation. Schema 2 retains the complete request and authenticated principal for offline recovery. Production policy-installation tooling, deletion/tombstones and native deployment bundles remain incomplete. None of these scripts are exposed as model tools or enabled native deployment paths.

OpenSSH documents [forced commands and key restrictions](https://man.openbsd.org/sshd.8#AUTHORIZED_KEYS_FILE_FORMAT). SQLite documents [synchronous durability settings](https://www.sqlite.org/pragma.html#pragma_synchronous). These are documented mechanisms; the actual fault tests and their limits are recorded separately in PLAN.md.

## Control-plane operation journal (NODE-002, partial)

`NodeExecutorTransport` uses a separate team-owned SSH credential, a pinned Ed25519 host key and a fixed `coolify-node-v1` command. It disables SSH config, agent, multiplexing and forwarding and sends typed JSON on stdin. The first profile excludes jump servers and Cloudflare tunnels rather than silently falling back to a broader transport. Temporary credential files have mode 0600 and are removed after use.

`SubmitNodeOperation` commits the original UUID, idempotency key, scope, generation, authority versions and deadline before dispatch. Workers recheck current administration and transport identity, claim a 90-second lease and fence late replies by attempt UUID. Process timeout is 65 seconds and job timeout is 80 seconds. Unknown transport outcomes remain uncertain; a subsequent denial does not erase a possibly successful effect. `node:reconcile`, scheduled every minute, redelivers at most 100 unresolved intents per sweep and stops automatic retries after five attempts. Explicit reconciliation remains available to current administrators. This is control-plane queue recovery; host-local backup scheduling is a later feature.

API routes under `/api/v1/servers/{uuid}/runtime-contexts/{context_uuid}`: `PATCH /executor` configures a preinstalled restricted endpoint, `GET /operations` lists recent scoped outcomes, `POST /operations` submits status/start/stop/restart, and `POST /operations/{operation_uuid}/reconcile` reuses the stored request. Server settings expose connection configuration and recent operation history. Changing connection settings does not install node policy or transfer ownership. No arbitrary shell, unit or path is accepted. Native workload registration is not enabled yet.

Read-only status returns the actual journal generation even when the caller has stale metadata. It cannot reset that generation or apply effects. Start/stop/restart still require an exact generation match. The disposable PHP integration test exercises actual restricted SSH, persisted outcomes, same-request replay, no-op start, generation discovery and rejection of a valid but mismatched host key. The broader lifecycle and interrupted restart gates remain the Python VM tests.

A new authorized principal may submit `recover` with the earlier lifecycle operation UUID. The executor checks the current epoch, policy, resource scope and generation, then re-observes the original intent using its recorded unit-definition fingerprint. It never repeats start/stop/restart. A changed definition or legacy missing fingerprint leaves the intent unresolved. Both original and recovery outcomes are journaled; the control plane adopts only the matching target within the same runtime/resource scope. This passed real SIGKILL + credential handoff and restored-controller tests. The old credential is denied; current fixture authority is epoch 2/policy 3/generation 6. This fixture-specific root policy transfer is test instrumentation, not a production remote policy administration endpoint.

Scoped `events` requests add a cursor and return up to 25 immutable outbox entries for one authorized resource. Read outcomes are journaled but do not themselves generate outbox events. `NodeEvent` stores received entries, rejects conflicting sequence reuse and prevents stale observations from regressing an operation. Event page ingestion and operation outcomes commit together. Durable per-resource cursors advance only through successfully ingested page boundaries, preserving missing history when pages arrive out of order. The server operation panel can request events; `GET .../runtime-contexts/{context_uuid}/events` exposes recent persisted history to current administrators.

The real outbox fixture transitioned the current principal to policy 4 (epoch 2 unchanged), retained generation 6 and delivered 13 historical events. The restored control-plane test receives pages in reverse order and verifies duplicate ingestion and cursor continuity. Older policy-1/2/3 fixture requests remain fenced; run the full sequence on a fresh fixture instead of changing their immutable requests. Automatic host-wide event subscription and installed backup-plan discovery remain future work.

Protocol 2 binds each new request to the probed execution UID and GID. Node policy declares the minimum accepted protocol and expected identity, checked against the executing account. New fixture bootstrap requires protocol 2; legacy decoding exists only for explicitly older trusted policies. A changed runtime identity also invalidates queued control-plane transport fingerprints. The real upgrade to policy 5 retained generation 6 and rejected wrong UID/GID and protocol-1 restart requests without changing invocation.

For native publication work, Podman 5.4.2 [Quadlet documentation](https://docs.podman.io/en/v5.4.2/markdown/podman-systemd.unit.5.html) documents rootless search paths, symlink support, generator validation with `QUADLET_UNIT_DIRS` and generated-unit boot behavior through `[Install]`. These are implementation inputs; atomic versioned-bundle publication and its interruption recovery are not yet established by the existing simple-file prototype.

## Native specification and bundle staging (RNT-001/003, partial)

`ApplicationSpec` currently normalizes the closed, versioned `prebuilt-v1` profile. It accepts an immutable registry-qualified image digest, command/entrypoint arrays, an HTTP readiness declaration, bounded CPU/memory, an internal network, one loopback listener, named volumes and non-reserved labels. It rejects unsupported semantics instead of silently dropping them. Environment/secret references, dependencies, build definitions and ingress remain unfinished; this profile is not the full specification's application model.

The PHP `QuadletCompiler` produces deterministic container/network/volume files. The node independently validates and renders the same spec through `QuadletBundle`; neither raw units nor host paths cross the executor boundary. Shared golden fixtures cover canonical JSON, Unicode line separators, quotes and backslashes. The protected native resource grant bounds registries, host ports, memory, CPU and volume count and pins the Podman version and generator digest. The current grant installer is explicitly disposable test instrumentation; production enrollment is not delivered.

`POST .../operations` with `action: stage` and `spec` persists the canonical request before queue dispatch. Protocol 2 binds UID/GID and limits the encoded request to 16,384 bytes. The node journals intent, validates with the pinned generator, fsyncs complete files outside Quadlet search paths and atomically names an immutable bundle by its hash. Reuse checks content, ownership, modes and hard links. The manifest records compiler, spec/bundle hashes and validation identity. A successful staging outcome retains the resource generation and explicitly reports `bundle.state: staged`; it does not select units, reload systemd or activate containers.

`NodeStagingValidator` checks artifact identity on direct replies, event ingestion and recovered local intents. Invalid or missing metadata cannot turn an uncertain stage into success. The real restricted-SSH gate passed with the Unicode bundle, rejected unscoped/raw/unsupported requests, replayed the same outcome, and confirmed no corresponding systemd unit existed. PHP reconciled the same saved operation through its pinned transport. See `PLAN.md` for exact checks and interruption evidence.

Activation remains a separate unmet gate. It must revalidate current runtime identity, selected artifacts, effective systemd units and existing object ownership. Podman's [pinned generator source](https://github.com/containers/podman/blob/v5.4.2/pkg/systemd/quadlet/quadlet.go) uses container replacement and ignores existing named network/volume creation. Therefore intended names/labels and a successful isolated generator run alone do not prove safe ownership. Application compensation must preserve persistent data and must not claim database recovery.
