ARG BUILD_FROM
FROM $BUILD_FROM

# Small shell only: everything heavy lives on the external drive.
RUN apk add --no-cache debootstrap binutils zstd xz tar wget ca-certificates \
        util-linux e2fsprogs exfatprogs fuse-exfat

COPY run.sh /run.sh
COPY rootfs/ /opt/printlab-files/
RUN chmod +x /run.sh

CMD [ "/run.sh" ]
