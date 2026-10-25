"""Keep server.json consistent with the image and the package version."""

import json
import re
import tomllib
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
SERVER = json.loads((ROOT / "server.json").read_text())


def test_oci_label_matches_server_name() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text()
    labels = re.findall(r'io\.modelcontextprotocol\.server\.name="([^"]+)"', dockerfile)
    assert labels == [SERVER["name"]]


def test_versions_match_pyproject() -> None:
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    (package,) = SERVER["packages"]
    assert SERVER["version"] == version
    assert package["registryType"] == "oci"
    assert package["identifier"].endswith(f":{version}")
    assert len(SERVER["description"]) <= 100


def test_package_runs_the_documented_container() -> None:
    (package,) = SERVER["packages"]
    env = {item["name"]: item for item in package["environmentVariables"]}
    assert env["SECRET_KEY"]["isRequired"] and env["SECRET_KEY"]["isSecret"]
    assert env["ENCRYPTION_KEY"]["isRequired"] and env["ENCRYPTION_KEY"]["isSecret"]
    transport = package["transport"]
    url = urlsplit(transport["url"])
    assert transport["type"] == "streamable-http"
    assert (url.hostname, url.path) == ("127.0.0.1", "/mcp")
    assert f"127.0.0.1:{url.port}:8000" in [arg["value"] for arg in package["runtimeArguments"]]
    (header,) = transport["headers"]
    assert header["name"] == "Authorization" and header["isSecret"]
