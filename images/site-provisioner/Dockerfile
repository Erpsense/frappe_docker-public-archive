# ERPSense multi-site provisioner image.
#
# Bakes site_provisioner.py + multisite-nginx.conf.template into the Frappe
# bench image so the script is durable across container restarts and VM
# disk resets. Replaces the volume-mount-from-host pattern.
#
# Usage (in compose):
#   site-provisioner:
#     image: asia-south1-docker.pkg.dev/erpsense-<env>/docker/frappe-multisite-provisioner:<sha>
#     entrypoint: ["python3", "/home/frappe/frappe-bench/sites/site_provisioner.py"]
#     environment:
#       MARIADB_ROOT_PASSWORD: ...
#       DEFAULT_ADMIN_PASSWORD: ...
#       MAX_SITES: "50"
#       POOL_SIZE: "5"
#       PROVISIONER_PORT: "9090"
#     volumes:
#       - sites:/home/frappe/frappe-bench/sites
#     ports:
#       - "9090:9090"
#
# Built and published by .github/workflows/build-publish-provisioner.yml on
# every push to main + on release tags.

ARG FRAPPE_VERSION=v15.96.1
FROM frappe/erpnext:${FRAPPE_VERSION}

# Both files are placed at the locations the provisioner expects (it reads
# ../sites siblings). USER is already `frappe` (UID 1000) in the upstream
# image; chown is defensive in case the base image changes.
COPY --chown=frappe:frappe site_provisioner.py /opt/erpsense/site_provisioner.py
COPY --chown=frappe:frappe multisite-nginx.conf.template /opt/erpsense/multisite-nginx.conf.template

# An init step copies these into the sites volume on first run if they're
# not already there. Keeping a symlink in /opt/erpsense as the source of
# truth means a `docker pull` of a new image actually rolls the script
# forward (volume-mounted files would shadow it).
COPY --chown=frappe:frappe images/site-provisioner/entrypoint.sh /opt/erpsense/entrypoint.sh
RUN chmod +x /opt/erpsense/entrypoint.sh

ENTRYPOINT ["/opt/erpsense/entrypoint.sh"]
CMD ["python3", "/home/frappe/frappe-bench/sites/site_provisioner.py"]
