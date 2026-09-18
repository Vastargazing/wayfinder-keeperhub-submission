"""Actual new HTTP client/parser with MockTransport, no live endpoints."""

import httpx
import pytest
from hosted_sepolia.hosted import HostedKeeperHub


@pytest.mark.parametrize(
    "out,raw,expected",
    [
        ({}, {}, "unknown"),
        ({}, {"sponsored": True}, "sponsored"),
        ({"sponsored": False}, {}, "direct"),
        ({"sponsored": True}, {}, "sponsored"),
        ({"sponsored": False}, {"sponsored": True}, "unknown"),
        ({"sponsored": False}, {"sponsored": False}, "direct"),
        ({"executedCall": {"sponsored": True}}, {}, "unknown"),
        ({"sponsored": True}, {"executedCall": {"sponsored": False}}, "unknown"),
    ],
)
def test_real_http_shapes(out, raw, expected):
    paths = []

    def handler(request):
        paths.append(request.url.path)
        if request.url.path.endswith("/logs"):
            return httpx.Response(
                200,
                json={
                    "logs": [
                        {
                            "nodeType": "web3/write-contract",
                            "nodeId": "action",
                            "output": out,
                            "outputRaw": raw,
                        }
                    ]
                },
            )
        return httpx.Response(404, json={"error": "Execution not found"})

    client = HostedKeeperHub(
        base_url="https://offline.invalid",
        api_key="fixture",
        transport=httpx.MockTransport(handler),
    )
    assert client.execution_mode("execution").mode.value == expected
    assert paths == ["/api/workflows/executions/execution/logs"]


@pytest.mark.parametrize(
    "synth,result,expected",
    [
        (False, None, "unknown"),
        (False, {}, "unknown"),
        (False, {"sponsored": False}, "direct"),
        (True, {"sponsored": True}, "sponsored"),
        (True, {"sponsored": False}, "unknown"),
        (False, {"sponsored": True}, "unknown"),
        (True, {"sponsored": True, "executedCall": {"sponsored": False}}, "unknown"),
    ],
)
def test_status_result_is_a_separate_persisted_source(synth, result, expected):
    paths = []

    def handler(request):
        paths.append(request.url.path)
        if request.url.path != "/api/execute/exec/status":
            return httpx.Response(404, json={"error": "Execution not found"})
        return httpx.Response(
            200,
            json={
                "executionId": "exec",
                "status": "success",
                "sponsored": synth,
                "result": result,
            },
        )

    client = HostedKeeperHub(
        base_url="https://offline.invalid",
        api_key="fixture",
        transport=httpx.MockTransport(handler),
    )
    assert client.direct_execution_mode("exec").mode.value == expected
    assert paths == ["/api/execute/exec/status"]


def test_same_string_id_does_not_join_different_execution_tables():
    paths = []

    def handler(request):
        paths.append(request.url.path)
        if request.url.path == "/api/execute/same-id/status":
            return httpx.Response(
                200,
                json={
                    "executionId": "same-id",
                    "status": "success",
                    "sponsored": False,
                    "result": {"sponsored": False},
                },
            )
        assert request.url.path == "/api/workflows/executions/same-id/logs"
        return httpx.Response(
            200,
            json={
                "logs": [
                    {
                        "nodeType": "web3/write-contract",
                        "nodeId": "action",
                        "output": {"sponsored": True},
                        "outputRaw": {"sponsored": True},
                    }
                ]
            },
        )

    client = HostedKeeperHub(
        base_url="https://offline.invalid",
        api_key="fixture",
        transport=httpx.MockTransport(handler),
    )
    assert client.workflow_execution_mode("same-id").mode.value == "sponsored"
    assert client.direct_execution_mode("same-id").mode.value == "direct"
    assert paths == [
        "/api/workflows/executions/same-id/logs",
        "/api/execute/same-id/status",
    ]
