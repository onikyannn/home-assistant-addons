# sing-box

sing-box proxy service for Home Assistant.

The add-on downloads a remote `config.json`, validates it with `sing-box check`, and stores it under `/data/config/config.json`. Runtime state is stored under `/data/state`.

## Configuration

```yaml
config_url: https://example.com/config.json
config_username: my-login # Optional; set together with config_password.
config_password: my-password # Optional; used for HTTP Basic Auth.
```

## Notes

- The URL should point to a valid sing-box JSON configuration.
- To download from a server protected by HTTP Basic Auth, set both `config_username` and `config_password`.
- If a fresh config cannot be downloaded after retries, the add-on starts with the cached config from the previous successful download.
- The add-on uses host networking and requires `/dev/net/tun`.
- The full config URL and credentials are not printed to logs.
