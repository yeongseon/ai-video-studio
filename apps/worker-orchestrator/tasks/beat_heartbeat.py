"""The periodic heartbeat message, consumed so it never piles up.

Liveness is recorded by the Beat process when it publishes this message (see celery_app), not
here: the local worker is a solo pool, and a worker busy with a long task must not make a
healthy Beat look dead.
"""

from celery_app import celery_app


@celery_app.task(name="beat_heartbeat", ignore_result=True)
def beat_heartbeat() -> None:
    return None
