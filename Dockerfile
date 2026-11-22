FROM alpine/git:2.49.1@sha256:c0280cf9572316299b08544065d3bf35db65043d5e3963982ec50647d2746e26 AS upstream
RUN git init /upstream && \
    git -C /upstream fetch --depth=1 https://github.com/tobymao/sqlglot.git eb11aec4be8cb2122afe713a4816662c3f0fb2fa && \
    test "$(git -C /upstream rev-parse FETCH_HEAD)" = eb11aec4be8cb2122afe713a4816662c3f0fb2fa && \
    mkdir /baseline && git -C /upstream archive FETCH_HEAD | tar -x -C /baseline

FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /entry
COPY --from=upstream /baseline /baseline
COPY --chmod=0444 selector.py executor_probe.py executor_replay.py executor_checkpoint.json.gz fixture_pairs.py patches.py semantics.py semantic_edits.py baseline.json.gz semantics.json.gz ./
USER 65534:65534
ENTRYPOINT ["python", "/entry/selector.py"]
