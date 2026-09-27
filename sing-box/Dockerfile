FROM ghcr.io/home-assistant/base:3.23

RUN apk add --no-cache \
    curl \
    iproute2 \
    jq \
    socat \
    tar

COPY config.yaml /tmp/addon-config.yaml

ARG TARGETARCH
ARG TARGETVARIANT

RUN set -eux && \
    singbox_version="$(awk -F: '$1 == "version" { value=$2; sub(/^[[:space:]]*/, "", value); sub(/[[:space:]]*#.*/, "", value); gsub(/^"|"$/, "", value); print value; exit }' /tmp/addon-config.yaml)" && \
    if [ -z "$singbox_version" ]; then echo "Unable to determine sing-box version from config.yaml" >&2; exit 1; fi && \
    arch="${TARGETARCH:-$(uname -m)}${TARGETVARIANT:-}" && \
    case "$arch" in \
        aarch64|arm64*) sb_arch="arm64" ;; \
        armv7l|armv7|armv7*) sb_arch="armv7" ;; \
        x86_64|amd64) sb_arch="amd64" ;; \
        *) echo "Unsupported architecture: $arch" && exit 1 ;; \
    esac && \
    curl -fL --retry 3 --retry-delay 2 -o /tmp/sing-box.tar.gz \
    "https://github.com/SagerNet/sing-box/releases/download/v${singbox_version}/sing-box-${singbox_version}-linux-${sb_arch}-musl.tar.gz" && \
    tar -xzf /tmp/sing-box.tar.gz -C /tmp && \
    cp /tmp/sing-box-${singbox_version}-linux-${sb_arch}-musl/sing-box /usr/local/bin/sing-box && \
    chmod +x /usr/local/bin/sing-box && \
    rm -rf /tmp/sing-box* /tmp/addon-config.yaml

COPY rootfs /

RUN chmod +x /etc/services.d/sing-box/run /etc/services.d/sing-box/finish \
    /etc/services.d/watchdog/run /usr/local/bin/watchdog-check /usr/local/bin/watchdog-http
