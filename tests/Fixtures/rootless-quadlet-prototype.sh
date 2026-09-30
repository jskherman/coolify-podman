#!/usr/bin/env bash
# Phase-0 experiment only: never installed by the application or offered as an agent tool.
set -euo pipefail

fixture_id=${1:?Expected disposable fixture operation ID}
phase=${2:?Expected install, verify, or recover}
[[ "$fixture_id" == coolify-fixture-* ]]
[[ "$(cat /etc/coolify-disposable-fixture)" == "$fixture_id" ]]
workload_uid=$(id -u workload)
unit_dir=/home/workload/.config/containers/systemd
cd /home/workload

workload() {
    runuser -u workload -- env XDG_RUNTIME_DIR="/run/user/$workload_uid" \
        DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$workload_uid/bus" "$@"
}

verify() {
    for attempt in $(seq 1 90); do
        if [[ "$(workload systemctl --user is-active coolify-phase0.service)" == active ]] && \
           [[ "$(curl --fail --silent http://127.0.0.1:18080/)" == "$fixture_id" ]]; then break; fi
        sleep 1
    done
    [[ "$(workload systemctl --user is-active coolify-phase0.service)" == active ]]
    [[ "$(curl --fail --silent http://127.0.0.1:18080/)" == "$fixture_id" ]]
    [[ "$(workload podman info --format '{{.Host.Security.Rootless}}')" == true ]]
    [[ "$(workload podman inspect systemd-coolify-phase0 --format '{{.State.Running}}')" == true ]]
    # The backend must not listen on the VM's externally reachable interface.
    address=$(hostname -I | awk '{print $1}')
    if curl --noproxy '*' --connect-timeout 2 --max-time 3 --fail --silent "http://$address:18080/"; then
        echo 'Unexpected non-loopback backend exposure' >&2
        exit 1
    fi
    printf 'rootless_quadlet_verified boot_id=%s\n' "$(cat /proc/sys/kernel/random/boot_id)"
}

case "$phase" in
install)
    install -d -m 0700 -o workload -g workload "$unit_dir"
    for existing in "$unit_dir/coolify-phase0.container" "$unit_dir/coolify-phase0.volume"; do
        if [[ -e "$existing" ]] && ! grep -Fqx "# $fixture_id" "$existing"; then
            echo 'Refusing to replace an unowned unit' >&2
            exit 1
        fi
    done
    cat > "$unit_dir/coolify-phase0.volume" <<EOF
# $fixture_id
[Volume]
VolumeName=coolify-phase0-data
EOF
    cat > "$unit_dir/coolify-phase0.container" <<EOF
# $fixture_id
[Container]
Image=docker.io/library/busybox@sha256:5cec3fc171c87218698e85a52af7087de727372aae264a787b8112901a5b0092
Volume=coolify-phase0.volume:/data
PublishPort=127.0.0.1:18080:8080
Exec=sh -c 'test -f /data/index.html || printf "$fixture_id" > /data/index.html; exec busybox httpd -f -p 8080 -h /data'
[Service]
Restart=on-failure
TimeoutStartSec=180
[Install]
WantedBy=default.target
EOF
    chown workload:workload "$unit_dir"/coolify-phase0.*
    chmod 0600 "$unit_dir"/coolify-phase0.*
    workload systemctl --user daemon-reload
    workload systemctl --user start coolify-phase0.service
    verify
    before=$(workload systemctl --user show coolify-phase0.service -p InvocationID --value)
    workload systemctl --user daemon-reload
    after=$(workload systemctl --user show coolify-phase0.service -p InvocationID --value)
    [[ "$before" == "$after" ]]
    echo 'unchanged_reload_preserved_invocation'
    ;;
verify)
    verify
    ;;
recover)
    verify
    cp "$unit_dir/coolify-phase0.container" "$unit_dir/coolify-phase0.container.previous"
    sed -i 's|^Exec=.*|Exec=/deliberately-missing-fixture-command|' "$unit_dir/coolify-phase0.container"
    sed -i 's/^Restart=.*/Restart=no/' "$unit_dir/coolify-phase0.container"
    workload systemctl --user daemon-reload
    workload systemctl --user restart coolify-phase0.service || true
    for attempt in $(seq 1 30); do
        if [[ "$(workload systemctl --user is-failed coolify-phase0.service)" == failed ]]; then break; fi
        sleep 1
    done
    [[ "$(workload systemctl --user is-failed coolify-phase0.service)" == failed ]]
    # Restore only the prior unit; never restore or overwrite the data volume.
    cp "$unit_dir/coolify-phase0.container.previous" "$unit_dir/coolify-phase0.container"
    workload systemctl --user daemon-reload
    workload systemctl --user reset-failed coolify-phase0.service
    workload systemctl --user start coolify-phase0.service
    verify
    echo 'failed_activation_compensated_data_preserved'
    ;;
*)
    echo 'Unknown prototype phase' >&2
    exit 1
    ;;
esac
