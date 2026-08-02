# sing-box add-on documentation

## Configuration

`config_url` is required and must point to a remote sing-box `config.json`.

Example:

```yaml
config_url: https://example.com/config.json
config_username: my-login # Optional; set together with config_password.
config_password: my-password # Optional; used for HTTP Basic Auth.
```

To download from an HTTP Basic Auth-protected endpoint, set both `config_username` and `config_password`. Leave both fields unset for endpoints that do not require authentication. The add-on does not log the URL or credentials.

On start, the add-on downloads the configuration to a temporary file, runs `sing-box check`, then atomically replaces the active config stored at `/data/config/config.json`.

If the new config cannot be downloaded after several attempts, or the downloaded config fails validation, the add-on starts with the existing cached config. If no cached config is available yet, startup fails.

## Runtime

The add-on runs with host networking and `NET_ADMIN` so sing-box can create TUN interfaces.
