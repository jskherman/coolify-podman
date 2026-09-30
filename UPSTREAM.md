# Upstream provenance

```yaml
upstream_repository: https://github.com/coollabsio/coolify
fork_repository: https://github.com/jskherman/coolify-podman
fork_base_commit: aef0f29b84fe40355fcccf03fd116711e2008c42
fork_base_date: 2026-09-28
license: Apache-2.0
upstream_tracking_branch: main
last_upstream_sync_commit: aef0f29b84fe40355fcccf03fd116711e2008c42
```

The SHA is the exact local starting commit (observed with `git log`), also verified through the official GitHub commits API on 2026-09-28. The upstream committer timestamp is 2026-09-27T23:18:01Z; the fork bootstrap date above is 2026-09-28. No upstream fetch or merge was performed. The only configured remote at bootstrap is the fork's `origin`.

`LICENSE` is preserved unchanged. No root NOTICE file exists at this base. Preserve dependency notices when packaging; the lockfiles record exact dependency source references. Existing Coolify attribution remains; the working fork name is not a final public brand. No Dokploy implementation has been copied. Any future reuse needs its exact commit, source paths, license and notices, excluding separately restricted content.

## Bootstrap seam inventory

| Surface | Inherited implementation | Intended treatment |
| --- | --- | --- |
| Deployment/build | `app/Jobs/ApplicationDeploymentJob.php`, `app/Actions/Application/GenerateConfig.php` | Adapt incrementally; separate OCI builds from runtime lifecycle |
| Application lifecycle | `app/Actions/Application/StopApplication.php`, `StopApplicationOneServer.php`, `StopApplicationPreview.php` | First runtime seam; retain state/events/callers |
| Status | `app/Actions/Docker/GetContainersStatus.php`, `app/Jobs/PushServerUpdateJob.php`, `bootstrap/helpers/docker.php` | Adapt discovery/inspection per runtime identity |
| Logs/terminal | `app/Services/TerminalSessionService.php`, `app/Livewire/Terminal`, `docker/coolify-terminal` | Preserve human workflows; never expose raw terminal as operational-agent tool |
| Cleanup | `app/Actions/Server/CleanupDocker.php` | Runtime-specific owned-resource cleanup; no automatic Podman translation |
| Database lifecycle | `app/Actions/Database`, `app/Services/DatabaseStartCommandExecutor.php` | Adapt lifecycle, preserve engine-specific configuration |
| Backup/restore | `app/Jobs/DatabaseBackupJob.php`, `app/Jobs/VolumeBackupJob.php`, scheduled backup models | Existing paths are not Restic/recovery compliance evidence |
| Proxy | `bootstrap/helpers/proxy.php`, `bootstrap/helpers/docker.php`, `app/Actions/Proxy`, `app/Livewire/Server/Proxy` | Preserve Docker Traefik/Caddy; native file drivers fork-owned |
| Service templates | `templates/compose`, `templates/service-templates-latest.json`, `bootstrap/helpers/parsers.php` | Preserve originals; explicit importer compatibility diagnostics |
| Metrics | `app/Actions/Server/StartSentinel.php`, `app/Services/SentinelTrafficClient.php` | Discover per-runtime capabilities; preserve existing Docker analytics |
| Installation/upgrade | `scripts/install.sh`, `scripts/upgrade.sh`, `docker-compose*.yml` | Inherit Docker control-plane packaging; do not run against existing host |
| Disposable hosts | `scripts/dev`, `app/Actions/Development/*Qemu*`, `tests/Feature/DevelopmentQemuVmTest.php` | Reuse after isolation/tooling review; requires booted systemd fixture |
| Authorization/API | model policies, `app/Http/Controllers/Api`, `routes/api.php` | Preserve team boundaries and add regression coverage per slice |

Unchanged areas are cleanly inherited. Extracted adapters are adapted upstream behavior. The restricted executor, native desired state/compiler, ingress publication protocol and recovery workflows will be fork-owned and documented when implemented. Upstream conflict resolution must preserve those boundaries.

## Dependency and CI inventory

`composer.lock` contains all 242 PHP packages with exact versions, source/dist references and declared licenses; `package-lock.json` contains 100 npm package records with resolved sources/integrity and declared licenses. Automated inspection found no missing license field in either inventory. This is a provenance inventory, not a distribution-license clearance: retain installed license files/notices in release packaging and review build-tool versus shipped-code obligations before publication. No dependencies were upgraded for this slice. Composer reports a pre-existing manifest content-hash mismatch; locked installation succeeds, but strict validation remains an unmet gate.

`.github/workflows/podman-fork-tests.yml` adds build/regression and real disposable-VM Docker checks with commit-pinned actions and read-only repository permissions. It does not publish images. Existing inherited release/publish workflows remain unchanged and require separate release authority. No hosted CI execution is claimed from authoring this workflow.
