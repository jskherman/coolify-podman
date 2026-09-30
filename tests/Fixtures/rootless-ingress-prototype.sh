#!/usr/bin/env bash
# Bounded phase-0 experiment, not the production ingress adapter.
set -euo pipefail
fixture_id=${1:?Expected disposable fixture operation ID}
phase=${2:?Expected install or verify}
[[ "$fixture_id" == coolify-fixture-* ]]
[[ "$(cat /etc/coolify-disposable-fixture)" == "$fixture_id" ]]
config_dir=/etc/coolify-phase0-ingress

verify() {
    for attempt in $(seq 1 90); do
        if [[ "$(systemctl is-active coolify-phase0-ingress)" == active ]] && \
           [[ "$(curl --fail --silent http://127.0.0.1:18081/)" == "$fixture_id" ]]; then break; fi
        sleep 1
    done
    [[ "$(systemctl is-active coolify-phase0-ingress)" == active ]]
    [[ "$(curl --fail --silent http://127.0.0.1:18081/)" == "$fixture_id" ]]
    # The workload identity must not administer its ingress protection.
    if runuser -u workload -- curl --silent --unix-socket /run/coolify-phase0-ingress/admin.sock http://localhost/config/; then
        echo 'Workload identity reached the ingress admin endpoint' >&2
        exit 1
    fi
    echo 'separate_identity_ingress_verified'
}

case "$phase" in
install)
    [[ "$(dpkg-query -W -f='${Version}' caddy)" == 2.6.2-12+deb13u1 ]]
    if [[ -d "$config_dir" ]] && [[ "$(cat "$config_dir/owner")" != "$fixture_id" ]]; then
        echo 'Refusing to replace unowned ingress' >&2
        exit 1
    fi
    install -d -m 0750 -o root -g caddy "$config_dir"
    printf '%s' "$fixture_id" > "$config_dir/owner"
    cat > "$config_dir/Caddyfile" <<'EOF'
{
    admin unix//run/coolify-phase0-ingress/admin.sock
    auto_https off
}
http://:18081 {
    reverse_proxy 127.0.0.1:18080
}
EOF
    chown root:caddy "$config_dir/Caddyfile"
    chmod 0640 "$config_dir/Caddyfile"
    runuser -u caddy -- caddy validate --adapter caddyfile --config "$config_dir/Caddyfile"
    cat > /etc/systemd/system/coolify-phase0-ingress.service <<EOF
# $fixture_id
[Unit]
After=network.target
[Service]
User=caddy
Group=caddy
RuntimeDirectory=coolify-phase0-ingress
RuntimeDirectoryMode=0700
ExecStart=/usr/bin/caddy run --adapter caddyfile --config $config_dir/Caddyfile
ExecReload=/usr/bin/caddy reload --adapter caddyfile --config $config_dir/Caddyfile --address unix//run/coolify-phase0-ingress/admin.sock
Restart=on-failure
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable --now coolify-phase0-ingress
    for attempt in $(seq 1 30); do
        if curl --fail --silent http://127.0.0.1:18081/ >/dev/null; then break; fi
        sleep 1
    done
    verify
    before=$(sha256sum "$config_dir/Caddyfile")
    printf ':18081 {\n    not_a_valid_directive\n}\n' > "$config_dir/rejected.Caddyfile"
    chown root:caddy "$config_dir/rejected.Caddyfile"
    chmod 0640 "$config_dir/rejected.Caddyfile"
    if runuser -u caddy -- caddy validate --adapter caddyfile --config "$config_dir/rejected.Caddyfile"; then
        echo 'Invalid candidate passed validation' >&2
        exit 1
    fi
    [[ "$before" == "$(sha256sum "$config_dir/Caddyfile")" ]]
    systemctl reload coolify-phase0-ingress
    verify
    echo 'rejected_candidate_preserved_route'
    ;;
verify)
    verify
    ;;
*) exit 1 ;;
esac
