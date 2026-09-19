FROM alpine:3.20

RUN apk add --no-cache bash curl jq

COPY init-users.sh /init-users.sh
RUN chmod +x /init-users.sh

ENTRYPOINT ["/init-users.sh"]
