from __future__ import annotations

import asyncio
from unittest.mock import ANY, AsyncMock, Mock

import pytest

from ace_net_manager.services.deploy_service import DeployService
from ace_net_manager.services.provision_service import ProvisionService
from ace_net_manager.services.restart_service import RestartService
from ace_net_manager.services.rollback_service import RollbackService
from ace_net_manager.settings import ManagerSettings
from ace_net_manager.utils.errors import OperationError


pytestmark = pytest.mark.unit


def _settings() -> ManagerSettings:
    return ManagerSettings(
        ace_internal_api_token="test-token",
        ace_docker_network="ace-net",
        ace_agent_internal_port=8000,
        ace_default_image_tag="ace-agent:latest",
        ace_container_startup_grace_seconds=0,
        ace_health_timeout_seconds=2,
        ace_health_poll_interval_seconds=1,
        ace_drain_timeout_seconds=1,
        ace_keep_previous_warm_seconds=0,
        ace_max_global_concurrent_ops=2,
        ace_docker_base_url="",
        ace_bind_host="127.0.0.1",
        ace_bind_port=8100,
        ace_enable_gateway_cutover=False,
        ace_preserve_failed_containers=False,
    )


def _db() -> Mock:
    db = Mock()
    db.commit = Mock()
    return db


def _docker() -> Mock:
    docker = Mock()
    docker.assert_container_matches = Mock()
    return docker


def test_provision_user_success():
    runtime = Mock()
    runtime.get_runtime.return_value = None
    docker = _docker()
    docker.create_container.return_value = "container-1"
    health = Mock()
    health.wait_until_ready = AsyncMock(return_value=True)
    service = ProvisionService(
        settings=_settings(),
        runtime_service=runtime,
        docker_service=docker,
        health_service=health,
    )

    result = asyncio.run(
        service.provision_user(
            _db(),
            user_id="d1f8fd22-c6ee-4387-b3cc-dd3913321ff4",
            image_tag="ace-agent:v1",
            env_overrides={},
        )
    )

    assert result["generation"] == 1
    runtime.update_runtime.assert_any_call(
        ANY,
        "d1f8fd22-c6ee-4387-b3cc-dd3913321ff4",
        status="active",
        current_generation=1,
        active_container_name=ANY,
        active_container_id="container-1",
        active_image_tag="ace-agent:v1",
        previous_generation=None,
        previous_container_name=None,
        previous_container_id=None,
        previous_image_tag=None,
        desired_image_tag=None,
        last_health_status="healthy",
        last_health_checked_at="now()",
    )


def test_provision_user_failure_marks_runtime_failed():
    runtime = Mock()
    runtime.get_runtime.return_value = None
    docker = _docker()
    docker.create_container.return_value = "container-1"
    health = Mock()
    health.wait_until_ready = AsyncMock(return_value=False)
    service = ProvisionService(
        settings=_settings(),
        runtime_service=runtime,
        docker_service=docker,
        health_service=health,
    )

    with pytest.raises(OperationError):
        asyncio.run(
            service.provision_user(
                _db(),
                user_id="9e247cd0-a233-4efa-b86d-bf5adb50a5c4",
                image_tag="ace-agent:v1",
                env_overrides={},
            )
        )

    docker.cleanup_failed_container.assert_called_once_with(
        ANY,
        timeout=1,
        preserve=False,
        preserved_name=ANY,
    )
    runtime.update_runtime.assert_any_call(
        ANY,
        "9e247cd0-a233-4efa-b86d-bf5adb50a5c4",
        status="failed",
        last_health_status="failed",
    )


def test_provision_user_failure_preserves_container_when_enabled():
    runtime = Mock()
    runtime.get_runtime.return_value = None
    docker = _docker()
    docker.create_container.return_value = "container-1"
    health = Mock()
    health.wait_until_ready = AsyncMock(return_value=False)
    settings = _settings()
    settings = ManagerSettings(**{**settings.__dict__, "ace_preserve_failed_containers": True})
    service = ProvisionService(
        settings=settings,
        runtime_service=runtime,
        docker_service=docker,
        health_service=health,
    )

    with pytest.raises(OperationError):
        asyncio.run(
            service.provision_user(
                _db(),
                user_id="c74c61c9-e0e1-4e22-a295-2a4fd716bce0",
                image_tag="ace-agent:v1",
                env_overrides={},
            )
        )

    docker.cleanup_failed_container.assert_called_once_with(
        ANY,
        timeout=1,
        preserve=True,
        preserved_name=ANY,
    )


def test_deploy_pre_cutover_failure_keeps_current_active():
    runtime = Mock()
    runtime.get_runtime.return_value = {
        "status": "active",
        "current_generation": 2,
        "volume_name": "ace_user_user_data",
        "network_name": "ace-net",
        "active_container_name": "ace-user-user",
        "active_image_tag": "ace-agent:v1",
    }
    docker = _docker()
    docker.create_container.return_value = "candidate-id"
    health = Mock()
    health.wait_until_ready = AsyncMock(side_effect=[False, True])
    service = DeployService(
        settings=_settings(),
        runtime_service=runtime,
        docker_service=docker,
        health_service=health,
    )

    with pytest.raises(OperationError):
        asyncio.run(
            service.deploy_user(
                _db(),
                user_id="0f278006-22fc-4d66-9d3d-b6a9a215f028",
                image_tag="ace-agent:v2",
                env_overrides={},
                keep_previous_warm_seconds=0,
                operation_id="op-1",
            )
        )

    runtime.atomic_cutover.assert_not_called()
    assert any(
        c.kwargs.get("status") == "active" and c.kwargs.get("desired_image_tag") is None
        for c in runtime.update_runtime.call_args_list
    )


def test_deploy_failure_restores_previous_active_when_candidate_unhealthy():
    runtime = Mock()
    runtime.get_runtime.return_value = {
        "status": "active",
        "current_generation": 2,
        "volume_name": "ace_user_user_data",
        "network_name": "ace-net",
        "active_container_name": "ace-user-user",
        "active_image_tag": "ace-agent:v1",
    }
    docker = _docker()
    docker.create_container.return_value = "candidate-id"
    health = Mock()
    health.wait_until_ready = AsyncMock(side_effect=[False, True])

    service = DeployService(
        settings=_settings(),
        runtime_service=runtime,
        docker_service=docker,
        health_service=health,
    )

    with pytest.raises(OperationError):
        asyncio.run(
            service.deploy_user(
                _db(),
                user_id="6bfdf31c-4a9f-4d1d-a947-cfdcf953e20b",
                image_tag="ace-agent:v2",
                env_overrides={},
                keep_previous_warm_seconds=0,
                operation_id="op-2",
            )
        )

    runtime.atomic_cutover.assert_not_called()
    assert docker.create_container.call_count == 2
    restored_call = docker.create_container.call_args_list[-1].kwargs
    assert restored_call["name"] == "ace-user-user"
    assert restored_call["image_tag"] == "ace-agent:v1"


def test_deploy_stops_old_before_starting_new_candidate_on_single_volume():
    runtime = Mock()
    runtime.get_runtime.return_value = {
        "status": "active",
        "current_generation": 2,
        "volume_name": "ace_user_user_data",
        "network_name": "ace-net",
        "active_container_name": "ace-user-user",
        "active_image_tag": "ace-agent:v1",
    }
    runtime.atomic_cutover.return_value = {
        "generation": 2,
        "container_name": "ace-user-user",
        "container_id": "old-id",
        "image_tag": "ace-agent:v1",
    }
    docker = _docker()
    docker.create_container.return_value = "candidate-active-id"
    health = Mock()
    health.wait_until_ready = AsyncMock(return_value=True)
    service = DeployService(
        settings=_settings(),
        runtime_service=runtime,
        docker_service=docker,
        health_service=health,
    )

    result = asyncio.run(
        service.deploy_user(
            _db(),
            user_id="59d8f3b5-cf6a-4bdf-a26e-feae63c328ba",
            image_tag="ace-agent:v2",
            env_overrides={"FEATURE_X": "true"},
            keep_previous_warm_seconds=0,
            operation_id="op-promote",
        )
    )

    assert result["new_generation"] == 3
    docker.stop_and_remove_container.assert_any_call("ace-user-user", timeout=1)
    assert docker.create_container.call_count == 1
    create_call = docker.create_container.call_args.kwargs
    assert create_call["environment"]["ACE_ROLE"] == "active"
    runtime.atomic_cutover.assert_called_once()


def test_deploy_retires_old_generation_immediately_after_success():
    runtime = Mock()
    runtime.get_runtime.return_value = {
        "status": "active",
        "current_generation": 2,
        "volume_name": "ace_user_user_data",
        "network_name": "ace-net",
        "active_container_name": "ace-user-user",
        "active_image_tag": "ace-agent:v1",
    }
    runtime.atomic_cutover.return_value = {
        "generation": 2,
        "container_name": "ace-user-user",
        "container_id": "old-id",
        "image_tag": "ace-agent:v1",
    }
    docker = _docker()
    docker.create_container.return_value = "candidate-active-id"
    health = Mock()
    health.wait_until_ready = AsyncMock(return_value=True)
    service = DeployService(
        settings=_settings(),
        runtime_service=runtime,
        docker_service=docker,
        health_service=health,
    )

    _ = asyncio.run(
        service.deploy_user(
            _db(),
            user_id="4f9549f7-f010-4ab8-a4a0-c4a6d6178f88",
            image_tag="ace-agent:v2",
            env_overrides={},
            keep_previous_warm_seconds=0,
            operation_id="op-no-retire",
        )
    )

    runtime.update_generation.assert_any_call(
        ANY,
        user_id="4f9549f7-f010-4ab8-a4a0-c4a6d6178f88",
        generation=2,
        role="retired",
        lifecycle_state="removed",
        retired_at="now()",
    )


def test_rollback_recreates_from_previous_image_on_single_volume():
    runtime = Mock()
    runtime.get_runtime.return_value = {
        "current_generation": 4,
        "previous_generation": 3,
        "previous_image_tag": "ace-agent:v1",
        "network_name": "ace-net",
        "volume_name": "ace_user_user_data",
        "active_container_name": "ace-user-user",
        "active_image_tag": "ace-agent:v2",
    }
    runtime.atomic_cutover.return_value = {
        "generation": 4,
        "container_name": "ace-user-user",
        "container_id": "g4-id",
        "image_tag": "ace-agent:v2",
    }
    runtime.get_generation.return_value = {
        "generation": 3,
        "docker_labels_json": {
            "env_overrides": {
                "ANTHROPIC_API_KEY": "secret",
            }
        },
        "image_tag": "ace-agent:v1",
    }
    docker = _docker()
    docker.create_container.return_value = "g5-id"
    health = Mock()
    health.wait_until_ready = AsyncMock(return_value=True)
    service = RollbackService(
        settings=_settings(),
        runtime_service=runtime,
        docker_service=docker,
        health_service=health,
    )

    result = asyncio.run(
        service.rollback_user(
            _db(),
            user_id="f6f7d849-dbe2-4c77-a367-3ab39aaf6cf0",
            reason="bad deploy",
            operation_id="op-3",
        )
    )

    assert result["recreated"] is True
    docker.stop_and_remove_container.assert_any_call("ace-user-user", timeout=1)
    docker.create_container.assert_called_once()
    create_call = docker.create_container.call_args.kwargs
    assert create_call["environment"]["ANTHROPIC_API_KEY"] == "secret"


def test_rollback_uses_previous_generation_image_when_runtime_missing_previous_image_tag():
    runtime = Mock()
    runtime.get_runtime.return_value = {
        "current_generation": 4,
        "previous_generation": 3,
        "previous_image_tag": None,
        "network_name": "ace-net",
        "volume_name": "ace_user_user_data",
        "active_container_name": "ace-user-user",
        "active_image_tag": "ace-agent:v2",
    }
    runtime.get_generation.return_value = {
        "generation": 3,
        "container_name": "ace-user-user",
        "container_id": "g3-id",
        "image_tag": "ace-agent:v1",
    }
    runtime.atomic_cutover.return_value = {
        "generation": 4,
        "container_name": "ace-user-user",
        "container_id": "g4-id",
        "image_tag": "ace-agent:v2",
    }
    docker = _docker()
    docker.create_container.return_value = "g5-id"
    health = Mock()
    health.wait_until_ready = AsyncMock(return_value=True)
    service = RollbackService(
        settings=_settings(),
        runtime_service=runtime,
        docker_service=docker,
        health_service=health,
    )

    _ = asyncio.run(
        service.rollback_user(
            _db(),
            user_id="07d834f4-2bcc-43fd-9052-f4f7e91809d6",
            reason="manual",
            operation_id="op-rollback-no-remove",
        )
    )

    create_call = docker.create_container.call_args.kwargs
    assert create_call["image_tag"] == "ace-agent:v1"


def test_restart_safe_reuses_active_generation_env_overrides():
    runtime = Mock()
    runtime.get_runtime.return_value = {
        "active_image_tag": "ace-agent:v9",
        "current_generation": 9,
    }
    runtime.get_generation.return_value = {
        "generation": 9,
        "docker_labels_json": {
            "env_overrides": {
                "ANTHROPIC_API_KEY": "secret",
            }
        },
    }
    deploy = Mock()
    deploy.deploy_user = AsyncMock(return_value={"new_generation": 10})
    service = RestartService(
        settings=_settings(),
        runtime_service=runtime,
        docker_service=Mock(),
        health_service=Mock(),
        deploy_service=deploy,
    )

    _ = asyncio.run(
        service.restart_user(
            _db(),
            user_id="3084cba8-21c7-4c48-8ac8-af44f4f45f87",
            mode="safe",
            operation_id="op-5",
        )
    )

    deploy.deploy_user.assert_called_once()
    assert deploy.deploy_user.call_args.kwargs["env_overrides"] == {"ANTHROPIC_API_KEY": "secret"}


def test_rollback_recreates_when_previous_container_missing():
    runtime = Mock()
    runtime.get_runtime.return_value = {
        "current_generation": 4,
        "previous_generation": 3,
        "previous_image_tag": "ace-agent:v1",
        "network_name": "ace-net",
        "volume_name": "ace_user_user_data",
        "active_container_name": "ace-user-user",
        "active_image_tag": "ace-agent:v2",
    }
    runtime.atomic_cutover.return_value = {
        "generation": 4,
        "container_name": "ace-user-user",
        "container_id": "g4-id",
        "image_tag": "ace-agent:v2",
    }
    docker = _docker()
    docker.create_container.return_value = "g5-id"
    health = Mock()
    health.wait_until_ready = AsyncMock(return_value=True)
    service = RollbackService(
        settings=_settings(),
        runtime_service=runtime,
        docker_service=docker,
        health_service=health,
    )

    result = asyncio.run(
        service.rollback_user(
            _db(),
            user_id="73a73f2f-58f7-4f85-b1ca-54885f2e8bdf",
            reason="container missing",
            operation_id="op-4",
        )
    )

    assert result["recreated"] is True
    docker.create_container.assert_called_once()


def test_restart_safe_delegates_to_deploy():
    runtime = Mock()
    runtime.get_runtime.return_value = {
        "active_image_tag": "ace-agent:v9",
        "current_generation": 9,
    }
    deploy = Mock()
    deploy.deploy_user = AsyncMock(return_value={"new_generation": 10})
    service = RestartService(
        settings=_settings(),
        runtime_service=runtime,
        docker_service=Mock(),
        health_service=Mock(),
        deploy_service=deploy,
    )

    result = asyncio.run(
        service.restart_user(
            _db(),
            user_id="3084cba8-21c7-4c48-8ac8-af44f4f45f87",
            mode="safe",
            operation_id="op-5",
        )
    )

    assert result["new_generation"] == 10
    deploy.deploy_user.assert_called_once()


def test_restart_force_requires_admin_requested_by():
    runtime = Mock()
    runtime.get_runtime.return_value = {
        "active_image_tag": "ace-agent:v1",
        "current_generation": 1,
        "active_container_name": "ace-user-user",
        "network_name": "ace-net",
        "volume_name": "ace_user_user_data",
    }
    service = RestartService(
        settings=_settings(),
        runtime_service=runtime,
        docker_service=Mock(),
        health_service=Mock(),
        deploy_service=Mock(),
    )

    with pytest.raises(OperationError) as exc:
        asyncio.run(
            service.restart_user(
                _db(),
                user_id="79f2f260-35d2-434c-ae49-ef9074d428f4",
                mode="force",
                operation_id="op-force",
                requested_by="gateway",
            )
        )
    assert exc.value.code == "forbidden_force_restart"


def test_cleanup_timed_out_deploy_removes_pre_cutover_candidate_and_resets_runtime():
    runtime = Mock()
    runtime.get_runtime.return_value = {
        "status": "deploying",
        "current_generation": 1,
        "previous_generation": None,
    }
    runtime.get_latest_generation.return_value = {
        "generation": 2,
        "container_name": "ace-user-user",
    }
    docker = _docker()
    health = Mock()
    service = DeployService(
        settings=_settings(),
        runtime_service=runtime,
        docker_service=docker,
        health_service=health,
    )

    result = asyncio.run(
        service.cleanup_timed_out_deploy(
            _db(),
            user_id="f1774131-00c8-4c18-b88a-a4efdf680e74",
            operation_id="op-timeout-cleanup",
        )
    )

    assert result == "pre_cutover_candidate_removed"
    docker.stop_and_remove_container.assert_called_once_with("ace-user-user", timeout=1)
    runtime.update_runtime.assert_any_call(
        ANY,
        "f1774131-00c8-4c18-b88a-a4efdf680e74",
        status="active",
        desired_image_tag=None,
        last_health_status="healthy",
    )
