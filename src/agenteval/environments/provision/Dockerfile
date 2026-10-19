# agenteval Docker environment: the same provisioning as the Tart VM image.
# Built by `agenteval env build <docker-env>`; context = this directory.
#
# The base is pinned by tag, which moves (`env build --force` re-pulls it). Runs record
# the built image ID and the provision.sh sha256 in the eval metadata, and the image
# carries /etc/agenteval/image.json, so results stay tied to the exact image used.
FROM ubuntu:24.04

ARG AGENT_USER=agent
ARG WORKSPACE=/workspace

COPY provision.sh /tmp/agenteval-provision.sh
RUN AGENTEVAL_AGENT_USER="${AGENT_USER}" AGENTEVAL_WORKSPACE="${WORKSPACE}" \
        bash /tmp/agenteval-provision.sh \
    && rm -f /tmp/agenteval-provision.sh

WORKDIR ${WORKSPACE}
CMD ["tail", "-f", "/dev/null"]
