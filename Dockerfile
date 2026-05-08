FROM ghcr.io/home-assistant/base:3.23

RUN apk add --no-cache \
    curl \
    jq \
    tar

ARG SINGBOX_VERSION=1.13.11
ARG TARGETARCH
ARG TARGETVARIANT

RUN set -eux && \
    arch="${TARGETARCH:-$(uname -m)}${TARGETVARIANT:-}" && \
    case "$arch" in \
        aarch64|arm64*) sb_arch="arm64" ;; \
        armv7l|armv7|armv7*) sb_arch="armv7" ;; \
        x86_64|amd64) sb_arch="amd64" ;; \
        *) echo "Unsupported architecture: $arch" && exit 1 ;; \
    esac && \
    curl -fL --retry 3 --retry-delay 2 -o /tmp/sing-box.tar.gz \
    "https://github.com/SagerNet/sing-box/releases/download/v${SINGBOX_VERSION}/sing-box-${SINGBOX_VERSION}-linux-${sb_arch}-musl.tar.gz" && \
    tar -xzf /tmp/sing-box.tar.gz -C /tmp && \
    cp /tmp/sing-box-${SINGBOX_VERSION}-linux-${sb_arch}-musl/sing-box /usr/local/bin/sing-box && \
    chmod +x /usr/local/bin/sing-box && \
    rm -rf /tmp/sing-box*

COPY rootfs /

RUN chmod +x /etc/services.d/sing-box/run && \
    chmod +x /etc/services.d/sing-box/finish
