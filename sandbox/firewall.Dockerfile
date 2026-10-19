FROM alpine:3.24
RUN apk add --no-cache iptables iptables-legacy
COPY sandbox/firewall.sh /firewall.sh
ENTRYPOINT ["/bin/sh", "/firewall.sh"]
