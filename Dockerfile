# Python 3.12, not 3.13: pymssql and duckdb both publish manylinux wheels for cp312,
# so this image builds with no compiler and no FreeTDS system package. A build that
# needs gcc is a build that can fail on the reviewer's machine for reasons that have
# nothing to do with this exercise.
FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies in their own layer so editing the pipeline does not re-resolve them.
COPY requirements.txt /app/requirements.txt
RUN python -m pip install -r /app/requirements.txt \
 && python -c "import pymssql, duckdb, yaml; print('drivers ok:', pymssql.__version__, duckdb.__version__)"

# The warehouse lives on a volume; create the mountpoint so a fresh `up` cannot fail
# on a missing directory before any of our own error handling runs.
RUN mkdir -p /warehouse /app/docs

# Copied as well as bind-mounted. The compose file mounts these read-only for fast
# iteration, but COPY means the image is self-contained and `docker run` works without
# compose -- which is what someone reaching for a debugger will actually type.
COPY pipeline /app/pipeline
COPY sql /app/sql
COPY tests /app/tests
# Evidence scripts. Not part of any command -- see probes/README.md.
COPY probes /app/probes

# `python -m pipeline` -- resolved via pipeline/__main__.py. Not a console_script
# entry point: that needs an install step, and an install step is another way for a
# clean checkout to fail.
ENTRYPOINT ["python", "-m", "pipeline"]
CMD ["demo"]
