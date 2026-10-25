# sing-box add-on documentation

## Configuration

`config_url` is required and must point to a remote sing-box `config.json`.

Example:

```yaml
config_url: https://example.com/config.json
config_username: my-login # Optional; set together with config_password.
config_password: my-password # Optional; used for HTTP Basic Auth.
healthcheck_url: https://www.gstatic.com/generate_204
internet_check_urls:
  - https://1.1.1.1/
  - https://8.8.8.8/
healthcheck_tolerance: 50
healthcheck_interval: 30
recovery_grace: 90
```

To download from an HTTP Basic Auth-protected endpoint, set both `config_username` and `config_password`. Leave both fields unset for endpoints that do not require authentication. The add-on does not log the URL or credentials.

On start, the add-on downloads the configuration to a temporary file, runs `sing-box check`, then atomically replaces the active config stored at `/data/config/config.json`.

If the new config cannot be downloaded after several attempts, or the downloaded config fails validation, the add-on starts with the existing cached config. If no cached config is available yet, startup fails.

### Recovery settings in the UI

The add-on's Configuration tab exposes the following fields, with English and
Russian labels. Save and restart the add-on to apply changes. Existing saved
options that omit these fields use the defaults below.

| Option | Default | Meaning |
| --- | --- | --- |
| `healthcheck_url` | `https://www.gstatic.com/generate_204` | HTTPS URL checked through each outbound |
| `internet_check_urls` | `https://1.1.1.1/`, `https://8.8.8.8/` | Nonempty list of HTTP/HTTPS URLs checked outside WireGuard |
| `healthcheck_tolerance` | 50 ms | Required latency advantage, 0–10000 ms |
| `healthcheck_interval` | 30 s | `INTERVAL`: cycle period, 5–3600 s |
| `recovery_grace` | 90 s | `GRACE`: tunnel recovery wait, 0–3600 s |

Direct Internet checks treat an HTTP response as evidence of reachability;
outbound checks measure delay. Use a small, reliably available response for
both. Request timeouts remain fixed at 5 seconds for outbound probes and 2 seconds
for each direct Internet request. The watchdog heartbeat continues at least every 30 seconds while waiting
for the next cycle, even when `healthcheck_interval` exceeds 120 seconds.
Domain names in direct Internet URLs are resolved with a 3-second DNS deadline.
The route check and HTTP request use the same resolved IP; curl retains the
original hostname and is bound to the selected interface. Failed DNS resolution
skips to the next configured URL. Literal IP URLs do not require DNS.

## Runtime

The add-on runs with host networking and `NET_ADMIN` so sing-box can create TUN interfaces.

## Automatic recovery

The add-on manages the **single tagged `selector`** in your configuration. Its
name and member tags can be arbitrary. If there are no selectors or more than
one, automatic selection and tunnel recovery are disabled with a log message;
sing-box still runs normally. Recovery timing and probe URLs are configurable
using the options above.

For example, use this group as your WireGuard endpoint's `detour`:

```json
{
  "type": "selector",
  "tag": "Selector",
  "outbounds": ["Hysteria2", "Naïve"],
  "default": "Hysteria2"
}
```

The controller reads `now` and `all` from Clash API each cycle and checks each
member using `healthcheck_url`, with a 5-second probe timeout.
By default, cycles start every 30 seconds, independently of proxy traffic. For
large groups that take longer, the next cycle starts after the previous one
finishes; cycles never overlap. Heartbeats also update as individual probes
finish, so a large group timing out does not make the watchdog stale.

- A failed current outbound switches to a working alternative after two
  consecutive confirmations of the same candidate.
- With a healthy tunnel, a faster outbound needs an advantage of at least
  `healthcheck_tolerance` (50 ms by default) in two consecutive checks. Latency
  optimization pauses during recovery.
- If every outbound is unavailable, the current selection is preserved.
- API errors reset pending decisions and do not count as outbound failures.
- The controller rechecks selection after measurements and before recovery, so a manual change
  during the probes is not overwritten. An observed manual change also starts
  a new recovery grace period.

On each add-on start, sing-box merges the cached source configuration and any
other config fragments into `/run/sing-box/config.json`. The add-on enables
`interrupt_exist_connections` on the managed selector. If Clash API has no
`external_controller`, it adds `127.0.0.1:9090`; an existing address, port,
secret and other API settings are preserved. API calls bypass environment
HTTP proxies and use the configured secret. The runtime copy is validated
before startup. The cached source configuration is not modified.

### WireGuard recovery

Tunnel recovery requires exactly one system WireGuard endpoint, using the
managed selector as its `detour`, with one peer, one local IPv4 address and
one allowed IPv4 subnet with prefix `/1` through `/30`. The VPS tunnel address
is assumed to be the subnet's first usable address, distinct from the local
address. Unsupported or ambiguous topology disables tunnel recovery while
outbound checks and switching continue.

The controller finds the tunnel interface by its local address and pings the
VPS through that interface. It allows `recovery_grace` (90 seconds by default) after startup or a selection
change for the tunnel to recover. After the grace period, three consecutive
failed tunnel checks permit a **sing-box service restart** only if the current
outbound and direct Internet access are both working. Direct Internet checks
use `internet_check_urls` (by default HTTPS to `1.1.1.1` or `8.8.8.8`), bound to the route's interface and excluding
the WireGuard interface. These checks test reachability, without certificate
verification, matching the previous watchdog behavior.

A service restart reuses the runtime config and does not download it again.
The recovery attempt is recorded in `/data/state/recovery.json` **before** the
restart and survives controller and add-on restarts. No further tunnel restart
is attempted during the same continuous outage. A successful tunnel probe
clears the record, allowing recovery of a future incident. An API failure,
missing Internet or failure of all outbounds never triggers a recovery restart.

### Supervisor watchdog

The HTTP endpoint on port `18787` only reads
`/run/sing-box/controller.json`. It returns 200 for a heartbeat no older than
120 seconds, including when the network is degraded or sing-box is starting.
A missing, malformed or stale heartbeat returns 503. HTTP requests never run
network probes, increment counters, switch outbounds or request recovery.

s6 supervises both sing-box and the controller and restarts crashed processes.
The recovery budget applies to intentional tunnel recovery; it does not stop
s6 from restarting a process that exits. Logs include measurements, selector
changes, skipped topology checks and the single recovery attempt.

## Development checks

```sh
python3 -m unittest discover -s sing-box/tests -v
docker build -t sing-box-addon:recovery-test sing-box
python3 sing-box/tests/docker_smoke.py
```

The Docker smoke test uses real s6 services, Clash API and HTTP watchdog in an
isolated container. Its outbound probe needs Internet access. WireGuard recovery
policy is covered by simulated network scenarios; the smoke test does not
establish a real WireGuard tunnel.
