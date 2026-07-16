# Pinned by digest, not by tag: `3.11-slim` is a moving target, and this repo's
# entire pitch is that the same inputs produce the same run on any machine.
FROM python:3.11-slim@sha256:baf89808ec37adeaab83cec287adb4a2afa4a11c1d51e961c7ec737877e61af6

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Deps first: they change far less often than the demo code, so this layer
# stays cached across the edits you'll actually be making.
COPY requirements.lock .
RUN pip install --no-cache-dir --require-hashes -r requirements.lock

COPY config.py agent.py client.py lanes.py health.py ./
COPY proxy/ proxy/
COPY mock/ mock/
COPY web/ web/

# The container binds the injector and the mock upstream to 127.0.0.1 only --
# they are internal demo plumbing, not services. 8080 is the one public port.
EXPOSE 8080

# Non-root: App Platform doesn't require it, but shipping a demo that a customer
# might fork as a starting point means not teaching them a bad habit.
RUN useradd -m -u 10001 demo && chown -R demo:demo /app
USER demo

# One worker on purpose. The run lock in web/server.py is per-process, and a
# second worker would let two runs share the injector's counters and desync.
CMD ["uvicorn", "web.server:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
