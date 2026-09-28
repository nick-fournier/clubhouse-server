#!/bin/sh
# Keep DNS in step with tunnel.yml, so routes only need editing there:
# - every hostname in tunnel.yml gets a proxied CNAME -> <TUNNEL_ID>.cfargotunnel.com
# - records that point at this tunnel but are no longer in tunnel.yml are deleted
# Records pointing anywhere else are never changed; a hostname that already
# points elsewhere is reported as a conflict (exit non-zero).
#
# Needs CF_API_TOKEN scoped to Zone:Read + DNS:Edit on the zone(s) used.
set -eu

apk add --no-cache -q curl jq >/dev/null

API=${CF_API_URL:-https://api.cloudflare.com/client/v4}
TARGET="${TUNNEL_ID:?TUNNEL_ID is not set}.cfargotunnel.com"
: "${CF_API_TOKEN:?CF_API_TOKEN is not set}"

cf() {
    curl -sS --fail-with-body -H "Authorization: Bearer $CF_API_TOKEN" \
        -H "Content-Type: application/json" "$@"
}

hosts=$(sed -n 's/^[[:space:]]*-[[:space:]]*hostname:[[:space:]]*"\{0,1\}\([^"[:space:]]*\)"\{0,1\}[[:space:]]*$/\1/p' /tunnel.yml | sort -u)

status=0
zones=""
for host in $hosts; do
    zone=$(echo "$host" | awk -F. '{print $(NF-1) "." $NF}')
    zone_id=$(cf "$API/zones?name=$zone" | jq -r '.result[0].id // empty')
    if [ -z "$zone_id" ]; then
        echo "ERROR    $host: zone $zone not found (token scope?)"
        status=1
        continue
    fi
    case " $zones " in *" $zone_id "*) ;; *) zones="$zones $zone_id" ;; esac

    record=$(cf "$API/zones/$zone_id/dns_records?name=$host" | jq -c '.result[0] // empty')
    if [ -z "$record" ]; then
        cf -X POST "$API/zones/$zone_id/dns_records" \
            -d "{\"type\":\"CNAME\",\"name\":\"$host\",\"content\":\"$TARGET\",\"proxied\":true,\"comment\":\"managed by clubhouse-server/razz/dns-sync.sh\"}" \
            >/dev/null
        echo "CREATED  $host -> $TARGET"
    elif [ "$(echo "$record" | jq -r '"\(.type) \(.content) \(.proxied)"')" = "CNAME $TARGET true" ]; then
        echo "OK       $host"
    else
        echo "CONFLICT $host: existing $(echo "$record" | jq -r '"\(.type) \(.content) proxied=\(.proxied)"'); left unchanged"
        status=1
    fi
done

# Prune: records pointing at this tunnel whose hostname was removed from tunnel.yml.
for zone_id in $zones; do
    cf "$API/zones/$zone_id/dns_records?type=CNAME&content=$TARGET&per_page=100" \
        | jq -r '.result[] | "\(.id) \(.name)"' \
        | while read -r id name; do
            if ! echo "$hosts" | grep -qxF "$name"; then
                cf -X DELETE "$API/zones/$zone_id/dns_records/$id" >/dev/null
                echo "DELETED  $name (not in tunnel.yml)"
            fi
        done
done

exit $status
