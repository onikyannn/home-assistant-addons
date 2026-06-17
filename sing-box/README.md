# sing-box

sing-box proxy service for Home Assistant.

The add-on downloads a remote `config.json`, validates it with `sing-box check`, and stores it under `/data/config/config.json`. Runtime state is stored under `/data/state`.

## Configuration

```yaml
config_url: https://example.com/config.json
```

## Notes

- The URL should point to a valid sing-box JSON configuration.
- If a fresh config cannot be downloaded after retries, the add-on starts with the cached config from the previous successful download.
- The add-on uses host networking and requires `/dev/net/tun`.
- The full config URL is not printed to logs.
