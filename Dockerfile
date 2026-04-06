ARG PYTHON_VERSION=3.11
ARG APP_UID=1000
ARG APP_GID=1000
ARG APP_USER=app
ARG APP_HOME=/home/app

FROM python:${PYTHON_VERSION}-slim AS runtime

ARG APP_UID=1000
ARG APP_GID=1000
ARG APP_USER=app
ARG APP_HOME=/home/app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=${APP_HOME} \
    PATH=${APP_HOME}/.local/bin:${PATH} \
    OUROBOROS_SERVER_HOST=0.0.0.0 \
    OUROBOROS_SERVER_PORT=8765 \
    OUROBOROS_REPO_DIR=/opt/ouroboros \
    OUROBOROS_DATA_DIR=${APP_HOME}/Ouroboros/data \
    OUROBOROS_FILE_BROWSER_DEFAULT=${APP_HOME}

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    git git-lfs vim curl wget && \
    rm -rf /var/lib/apt/lists/*

RUN groupadd -g ${APP_GID} ${APP_USER} && \
    useradd -m -d ${APP_HOME} -u ${APP_UID} -g ${APP_GID} -s /bin/bash ${APP_USER} && \
    mkdir -p ${APP_HOME}/Ouroboros /opt/ouroboros && \
    chown -R ${APP_UID}:${APP_GID} ${APP_HOME} /opt/ouroboros

WORKDIR /opt/ouroboros

COPY --chown=${APP_UID}:${APP_GID} . /opt/ouroboros

USER ${APP_USER}

RUN python -m pip install --user --no-cache-dir --upgrade pip setuptools wheel && \
    python -m pip install --user --no-cache-dir .

EXPOSE 8765
ENTRYPOINT ["ouroboros-web"]
