from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[3]


def test_runtime_acceptance_requires_one_running_healthy_beat() -> None:
    # Given the container acceptance, without starting Docker.
    acceptance = (ROOT / "scripts/cutover_runtime_acceptance.sh").read_text()
    # When inspecting the shared positive/negative health predicate.
    helper = re.search(r"beat_is_healthy\(\) \{\n(.*?)\n\}", acceptance, re.DOTALL)
    # Then absent, duplicate, stopped and unhealthy schedulers cannot pass.
    assert helper is not None
    assert "docker compose ps -a -q beat" in helper[1]
    assert "wc -l" in helper[1] and '= 1 ]' in helper[1]
    assert "{{.State.Running}}:{{.State.Health.Status}}" in helper[1]
    assert "grep -Fx 'true:healthy'" in helper[1]
    assert "DEADLINE=$((SECONDS + 150))\nuntil beat_is_healthy; do" in acceptance
    assert re.search(r'if .*SECONDS.*DEADLINE.*; then\n\s+docker compose logs beat\n\s+fail ', acceptance)


def test_runtime_acceptance_checks_singleton_after_repeated_supported_up() -> None:
    # Given / When: the supported workflow must be exercised, not just described.
    acceptance = (ROOT / "scripts/cutover_runtime_acceptance.sh").read_text()
    # Then both repeated Make invocations must preserve a healthy singleton.
    assert re.search(
        r'for attempt in 1 2; do\n\s+make docker-local-up\n\s+beat_is_healthy .*\|\| fail ',
        acceptance,
    )


def test_runtime_acceptance_requires_scheduler_dispatch_not_schedule_listing() -> None:
    # Given / When: inspect the real reconciliation evidence.
    acceptance = (ROOT / "scripts/cutover_runtime_acceptance.sh").read_text()
    # Then the DB transition is paired with Celery's actual due-task dispatch log.
    assert "SELECT status FROM creator_run_tasks WHERE celery_task_id='cutover-stale-${RUN_ID}'" in acceptance
    assert "grep -F 'Scheduler: Sending due task reconcile-stale-dispatches (reconcile_stale_dispatches)'" in acceptance


def test_runtime_acceptance_rejects_stopped_beat_even_with_a_fresh_receipt() -> None:
    # Given / When: inspect the negative runtime scenario after reconciliation.
    acceptance = (ROOT / "scripts/cutover_runtime_acceptance.sh").read_text()
    # Then stopping Beat must make the very same acceptance predicate reject it.
    assert re.search(
        r'docker compose stop -t 10 beat\nif beat_is_healthy; then\n\s+fail ', acceptance,
    )
    assert acceptance.index("docker compose stop -t 10 beat") > acceptance.index("RUN_STATE=")


def test_runtime_acceptance_writes_queue_state_from_every_container_topology() -> None:
    # Given / When
    acceptance = (ROOT / "scripts/cutover_runtime_acceptance.sh").read_text(
        encoding="utf-8"
    )

    # Then
    assert "run --rm --no-deps beat sh -c" in acceptance
    assert "/var/lib/short-form-studio/dlq/observed-broker-epoch" in acceptance
    for service in (
        "worker",
        "worker-script",
        "worker-image",
        "worker-audio",
        "worker-render",
    ):
        assert f'assert_dlq_write "{service}"' in acceptance


def test_runtime_acceptance_executes_all_redis_lifecycle_scenarios() -> None:
    # Given / When
    acceptance = (ROOT / "scripts/cutover_runtime_acceptance.sh").read_text(
        encoding="utf-8"
    )

    # Then
    for scenario in (
        "redis_process_restart",
        "redis_force_recreate_retained_volume",
        "redis_fresh_volume_replacement",
    ):
        assert f'run_redis_lifecycle "{scenario}"' in acceptance
    assert "for status in pending queued running success" in acceptance
    assert "${scenario}-lifecycle-${status}" in acceptance
    assert "assert_late_worker_fenced" in acceptance
    assert "workspace_quota_reservation_owners" in acceptance


def test_runtime_acceptance_preserves_real_routed_messages() -> None:
    # Given / When
    acceptance = (ROOT / "scripts/cutover_runtime_acceptance.sh").read_text(
        encoding="utf-8"
    )

    # Then
    assert "enqueue_lifecycle_marker" in acceptance
    assert "acceptance.marker" in acceptance
    assert "durability-marker" in acceptance
    assert "assert_lifecycle_marker" in acceptance


def test_runtime_acceptance_recovers_redacted_dlq_fallback_after_recreate() -> None:
    # Given / When
    acceptance = (ROOT / "scripts/cutover_runtime_acceptance.sh").read_text(
        encoding="utf-8"
    )

    # Then
    assert "_record_failed_task_to_dlq" in acceptance
    assert "read_dlq_fallback" in acceptance
    assert "--force-recreate --no-deps worker" in acceptance
    assert '"password": "<redacted>"' in acceptance
