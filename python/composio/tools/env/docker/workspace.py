"""
Docker workspace.
"""

import os
import socket
import time
import typing as t
from dataclasses import dataclass
from pathlib import Path

import requests
from docker import DockerClient, from_env
from docker.errors import DockerException, NotFound
from docker.models.containers import Container

from composio.exceptions import ComposioSDKError
from composio.tools.env.base import RemoteWorkspace, WorkspaceConfigType
from composio.tools.env.constants import ENV_COMPOSIO_DEV_MODE, ENV_COMPOSIO_SWE_AGENT


COMPOSIO_PATH = Path(__file__).parent.parent.parent.parent.parent.resolve()
COMPOSIO_CACHE = Path.home() / ".composio"
CONTAINER_DEV_VOLUMES = {
    COMPOSIO_PATH: {
        "bind": "/opt/composio-core",
        "mode": "rw",
    },
    COMPOSIO_CACHE: {
        "bind": "/root/.composio",
        "mode": "rw",
    },
}
DEV_MODE = os.environ.get(ENV_COMPOSIO_DEV_MODE, "0") == "1"
DEFAULT_IMAGE = "angrybayblade/composio"
DEFAULT_PORT = 54321


def _get_free_port() -> int:
    sock = socket.socket()
    try:
        sock.bind(("", 0))
        return sock.getsockname()[1]
    finally:
        sock.close()


@dataclass
class Config(WorkspaceConfigType):
    """Docker configuration type."""

    image: t.Optional[str] = None
    """Name of the docker image."""

    ports: t.Optional[t.Dict[int, t.Any]] = None
    """
    Ports to bind inside the container

    Note: port 8000 is reserved for the tooling server inside the container
    """

    volumes: t.Optional[t.Dict[str, t.Any]] = None
    """Voluems to bind inside the container"""


class DockerWorkspace(RemoteWorkspace):
    """Docker workspace implementation."""

    _port: int  # Tooling server port
    _container: Container
    _client: t.Optional[DockerClient] = None

    def __init__(self, config: Config) -> None:
        """Create a docker workspace."""
        super().__init__(config=config)
        self.image = config.image or os.environ.get(
            ENV_COMPOSIO_SWE_AGENT,
            DEFAULT_IMAGE,
        )
        self._port_requests = config.ports or {}
        self._volume_requests = config.volumes or {}

    def setup(self) -> None:
        """Setup docker workspace."""
        self.logger.debug(f"Creating docker workspace with image: {self.image}")
        self._port = _get_free_port()
        self.url = f"http://localhost:{self._port}/api"

        container_kwargs: t.Dict[str, t.Any] = {
            "tty": True,
            "detach": True,
            "stdin_open": True,
            "auto_remove": False,
            "image": self.image,
            "name": self.id,
            "environment": self.environment,
            "command": "/root/entrypoint.sh",
            "ports": {
                **self._port_requests,
                8000: self._port,
            },
            "volumes": self._volume_requests,
        }
        if DEV_MODE:
            container_kwargs["volumes"].update(CONTAINER_DEV_VOLUMES)  # type: ignore
            container_kwargs["environment"][ENV_COMPOSIO_DEV_MODE] = "1"

        try:
            self._container = self.client.containers.run(**container_kwargs)
            self._container.start()
            self._container.reload()
        except DockerException as e:
            raise ComposioSDKError("Error starting workspace: ", e) from e

        self._wait()

        # Setup network config
        self.host = "localhost"
        self.ports = [
            int(port[0]["HostPort"])
            for port in self._container.ports.values()
            if int(port[0]["HostPort"]) != self._port
        ]

    def _wait(self) -> None:
        """Wait for docker workspace to get started."""
        while True:
            try:
                if self._request(endpoint="", method="get").status_code == 200:
                    return
            except requests.ConnectionError:
                time.sleep(0.1)

    @property
    def client(self) -> DockerClient:
        """Docker client object."""
        if self._client is None:
            try:
                self._client = from_env(timeout=100)
            except DockerException as e:
                raise ComposioSDKError(
                    message=f"Error initializing docker client: {e}. "
                    "Please make sure docker is running and try again."
                ) from e
        return self._client

    def teardown(self) -> None:
        """Teardown docker workspace factory."""
        super().teardown()
        try:
            self._container.kill()
            self._container.remove()
        except NotFound as e:
            self.logger.debug(f"Error cleaning {self.id} - {e}")
