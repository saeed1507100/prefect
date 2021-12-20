import asyncio
import os
import subprocess
import sys
import threading
import warnings
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Dict,
    List,
    Optional,
    Sequence,
    Tuple,
    Type,
    TypeVar,
    Union,
)
from uuid import UUID

import anyio
import anyio.abc
import docker
import packaging.version
import sniffio
from anyio.abc import TaskStatus
from anyio.streams.text import TextReceiveStream
from pydantic import BaseModel, Field, root_validator, validator
from slugify import slugify
from typing_extensions import Literal

import prefect
from prefect.client import OrionClient
from prefect.orion.schemas.core import FlowRun, FlowRunnerSettings
from prefect.utilities.asyncio import run_sync_in_worker_thread
from prefect.utilities.compat import ThreadedChildWatcher
from prefect.utilities.logging import get_logger

if TYPE_CHECKING:
    from docker import DockerClient
    from docker.models.containers import Container


_FLOW_RUNNERS: Dict[str, "FlowRunner"] = {}
FlowRunnerT = TypeVar("FlowRunnerT", bound=Type["FlowRunner"])


DOCKER_BUILD_LOCK = threading.Lock()


class FlowRunner(BaseModel):
    """
    Flow runners are responsible for creating infrastructure for flow runs and starting
    execution.

    This base implementation manages casting to and from the API representation of
    flow runner settings and defines the interface for `submit_flow_run`. It cannot
    be used to run flows.
    """

    typename: str

    def to_settings(self) -> FlowRunnerSettings:
        return FlowRunnerSettings(
            type=self.typename, config=self.dict(exclude={"typename"})
        )

    @classmethod
    def from_settings(cls, settings: FlowRunnerSettings) -> "FlowRunner":
        subcls = lookup_flow_runner(settings.type)
        return subcls(**(settings.config or {}))

    @property
    def logger(self):
        return get_logger(f"flow_runner.{self.typename}")

    async def submit_flow_run(
        self,
        flow_run: FlowRun,
        task_status: TaskStatus,
    ) -> Optional[bool]:
        """
        Implementions should:

        - Create flow run infrastructure.
        - Start the flow run within it.
        - Call `task_status.started()` to indicate that submission was successful

        The method can then exit or continue monitor the flow run asynchronously.

        The method _may_ return a boolean indicating successful completion of the run.
        This return value is not intended for general consumption and is primarily
        useful for testing.
        """
        raise NotImplementedError()

    class Config:
        extra = "forbid"


def register_flow_runner(cls: FlowRunnerT) -> FlowRunnerT:
    _FLOW_RUNNERS[cls.__fields__["typename"].default] = cls
    return cls


def lookup_flow_runner(typename: str) -> FlowRunner:
    """Return the flow runner class for the given `typename`"""
    try:
        return _FLOW_RUNNERS[typename]
    except KeyError:
        raise ValueError(f"Unregistered flow runner {typename!r}")


@register_flow_runner
class UniversalFlowRunner(FlowRunner):
    """
    The universal flow runner contains configuration options that can be used by any
    Prefect flow runner implementation.

    This flow runner cannot be used at runtime and should be converted into a subtype.

    Attributes:
        env: Environment variables to provide to the flow run
    """

    typename: Literal["universal"] = "universal"
    env: Dict[str, str] = Field(default_factory=dict)

    async def submit_flow_run(
        self,
        flow_run: FlowRun,
        task_status: TaskStatus,
    ) -> Optional[bool]:
        raise RuntimeError(
            "The universal flow runner cannot be used to submit flow runs. If a flow "
            "run has a universal flow runner, it should be updated to the default "
            "runner type by the agent or user."
        )


@register_flow_runner
class SubprocessFlowRunner(UniversalFlowRunner):
    """
    Executes flow runs in a local subprocess.

    Attributes:
        stream_output: Stream output from the subprocess to local standard output
        condaenv: An optional name of an anaconda environment to run the flow in.
            A path can be provided instead, similar to `conda --prefix ...`.
        virtualenv: An optional path to a virtualenv environment to run the flow in.
            This also supports the python builtin `venv` environments.

    """

    typename: Literal["subprocess"] = "subprocess"
    stream_output: bool = False
    condaenv: Union[str, Path] = None
    virtualenv: Path = None

    @validator("condaenv")
    def coerce_pathlike_string_to_path(cls, value):
        if (
            not isinstance(value, Path)
            and value is not None
            and (value.startswith(os.sep) or value.startswith("~"))
        ):
            value = Path(value)
        return value

    @root_validator
    def ensure_only_one_env_was_given(cls, values):
        if values.get("condaenv") and values.get("virtualenv"):
            raise ValueError(
                "Received incompatible settings. You cannot provide both a conda and "
                "virtualenv to use."
            )
        return values

    async def submit_flow_run(
        self,
        flow_run: FlowRun,
        task_status: TaskStatus,
    ) -> Optional[bool]:

        if sys.version_info < (3, 8) and sniffio.current_async_library() == "asyncio":
            # Python < 3.8 does not use a `ThreadedChildWatcher` by default which can
            # lead to errors in tests on unix as the previous default `SafeChildWatcher`
            # is not compatible with threaded event loops.
            asyncio.get_event_loop_policy().set_child_watcher(ThreadedChildWatcher())

        # Open a subprocess to execute the flow run
        self.logger.info(f"Opening subprocess for flow run '{flow_run.id}'...")

        command, env = self._generate_command_and_environment(flow_run.id)

        self.logger.debug(f"Using command: {' '.join(command)}")

        process_context = await anyio.open_process(
            command,
            stderr=subprocess.STDOUT,
            env=env,
        )

        # Mark this submission as successful
        task_status.started()

        # Wait for the process to exit
        # - We must the output stream so the buffer does not fill
        # - We can log the success/failure of the process

        async with process_context as process:
            async for text in TextReceiveStream(process.stdout):
                if self.stream_output:
                    print(text, end="")  # Output is already new-line terminated

        if process.returncode:
            self.logger.error(
                f"Subprocess for flow run '{flow_run.id}' exited with bad code: "
                f"{process.returncode}"
            )
        else:
            self.logger.info(f"Subprocess for flow run '{flow_run.id}' exited cleanly.")

        return not process.returncode

    def _generate_command_and_environment(
        self, flow_run_id: UUID
    ) -> Tuple[Sequence[str], Dict[str, str]]:
        # Copy the base environment
        env = os.environ.copy()

        # Set up defaults
        command = []
        python_executable = sys.executable

        if self.condaenv:
            command += ["conda", "run"]
            if isinstance(self.condaenv, Path):
                command += ["--prefix", str(self.condaenv.expanduser().resolve())]
            else:
                command += ["--name", self.condaenv]

            python_executable = "python"

        elif self.virtualenv:
            # This reproduces the relevant behavior of virtualenv's activation script
            # https://github.com/pypa/virtualenv/blob/main/src/virtualenv/activation/bash/activate.sh

            virtualenv_path = self.virtualenv.expanduser().resolve()
            python_executable = str(virtualenv_path / "bin" / "python")
            # Update the path to include the bin
            env["PATH"] = str(virtualenv_path / "bin") + os.pathsep + env["PATH"]
            env.pop("PYTHONHOME", None)
            env["VIRTUAL_ENV"] = str(virtualenv_path)

        # Add `prefect.engine` call
        command += [
            python_executable,
            "-m",
            "prefect.engine",
            flow_run_id.hex,
        ]

        # Override with any user-provided variables
        env.update(self.env)

        return command, env


@register_flow_runner
class DockerFlowRunner(UniversalFlowRunner):
    typename: Literal["docker"] = "docker"

    image: str = None
    networks: List[str] = Field(default_factory=list)
    labels: Dict[str, str] = None
    auto_remove: bool = False
    stream_output: bool = True

    async def submit_flow_run(
        self,
        flow_run: FlowRun,
        task_status: TaskStatus,
    ) -> Optional[bool]:
        # The `docker` library uses requests instead of an async http library so it must
        # be run in a thread to avoid blocking the event loop.
        container_id = await run_sync_in_worker_thread(
            self._create_and_start_container, flow_run
        )

        # Mark as started
        task_status.started()

        # Monitor the container
        await run_sync_in_worker_thread(self._watch_container, container_id)

    def _create_and_start_container(self, flow_run: FlowRun) -> str:

        docker_client = self._get_client()

        container = self._create_container(
            docker_client,
            image=self._get_image(docker_client),
            network=self.networks[0] if self.networks else None,
            command=self._get_start_command(flow_run),
            environment=self._get_environment_variables(),
            auto_remove=self.auto_remove,
            labels=self._get_labels(flow_run),
            extra_hosts=self._get_extra_hosts(docker_client),
            name=self._get_container_name(flow_run),
            volumes=[f"{prefect.settings.home}:/root"],
        )

        # Add additional networks after the container is created; only one network can
        # be attached at creation time
        if len(self.networks) > 1:
            for network_name in self.networks[1:]:
                network = docker_client.networks.get(network_name)
                network.connect(container)

        # Start the container
        container.start()

        return container.id

    def _create_container(self, docker_client: "DockerClient", **kwargs) -> "Container":
        """
        Create a docker container with retries on name conflicts.

        If the container already exists with the given name, an incremented index is
        added.
        """
        # Create the container with retries on name conflicts (with an incremented idx)
        index = 0
        container = None
        name = original_name = kwargs.pop("name", "prefect-flow-run")
        while not container:
            try:
                container = docker_client.containers.create(name=name, **kwargs)
            except docker.errors.APIError as exc:
                if "Conflict" in str(exc) and "container name" in str(exc):
                    index += 1
                    name = f"{original_name}-{index}"
                else:
                    raise

        return container

    def _watch_container(self, container_id: str) -> bool:
        docker_client = self._get_client()

        try:
            container = docker_client.containers.get(container_id)
        except docker.errors.ImageNotFound:
            self.logger.error(f"Flow run container {container_id!r} was removed.")

        status = container.status
        self.logger.info(
            f"Flow run container {container.name!r} has status {container.status!r}"
        )

        for log in container.logs(stream=True):
            log: bytes
            if self.stream_output:
                print(log.decode().rstrip())

        container.reload()
        if container.status != status:
            self.logger.info(
                f"Flow run container {container.name!r} has status {container.status!r}"
            )

    def _get_client(self):
        try:
            docker_client = docker.from_env()
        except docker.errors.DockerException as exc:
            raise RuntimeError(f"Could not connect to Docker.") from exc

        return docker_client

    @staticmethod
    def _get_orion_image_tag():
        return slugify(
            f"prefect:orion-{prefect.__version__}",
            lowercase=False,
            max_length=128,
            # Docker allows these characters for tag names
            regex_pattern=r"[^a-zA-Z0-9_.-]+",
        )

    def _get_image(self, docker_client: "DockerClient"):
        """
        Retrieve the specified image, or build the orion image.
        """
        if self.image:
            return self.image

        # Ensure the orion image is built
        # Lock so that we do not try to build it if another thread is already doing so
        orion_image = self._get_orion_image_tag()
        self.logger.debug(f"No image provided. Using image {orion_image!r}...")
        with DOCKER_BUILD_LOCK:
            try:
                docker_client.images.get(orion_image)
            except docker.errors.ImageNotFound:
                self.logger.info(f"Orion image {orion_image!r} not found! Building...")
                docker_client.images.build(
                    path=str(prefect.__root_path__), tag=orion_image
                )

        return orion_image

    def _get_container_name(self, flow_run: FlowRun) -> str:
        """
        Generatse a container name to match the flow run name, ensuring it is docker
        compatible and unique.
        """
        # Must match `/?[a-zA-Z0-9][a-zA-Z0-9_.-]+` in the end

        return (
            slugify(
                flow_run.name,
                lowercase=False,
                # Docker does not limit length but URL limits apply eventually so
                # limit the length for safety
                max_length=250,
                # Docker allows these characters for container names
                regex_pattern=r"[^a-zA-Z0-9_.-]+",
            ).lstrip(
                # Docker does not allow leading underscore, dash, or period
                "_-."
            )
            # Docker does not allow 0 character names so use the flow run id if name
            # would be empty after cleaning
            or flow_run.id
        )

    def _get_start_command(self, flow_run: FlowRun) -> List[str]:
        return [
            "python",
            "-m",
            "prefect.engine",
            f"{flow_run.id}",
        ]

    def _get_extra_hosts(self, docker_client) -> Dict[str, str]:
        """
        A host.docker.internal -> host-gateway mapping is necessary for communicating
        with the API on Linux machines
        """
        user_version = packaging.version.parse(docker_client.version()["Version"])
        required_version = packaging.version.parse("20.10.0")

        if user_version < required_version:
            warnings.warn(
                "`host.docker.internal` could not be automatically resolved to your "
                "local host. This feature is not supported on Docker Engine "
                f"v{user_version}, upgrade to v{required_version}+ if you "
                "encounter issues."
            )
        else:
            # Compatibility for linux -- https://github.com/docker/cli/issues/2290
            # Only supported by Docker v20.10.0+ which is our minimum recommend version
            return {"host.docker.internal": "host-gateway"}

    def _get_environment_variables(self):
        env = self.env.copy()

        # Convert local connections to use the docker host

        if prefect.settings.orion_host:
            api_url = prefect.settings.orion_host.replace(
                "localhost", "host.docker.internal"
            ).replace("127.0.0.1", "host.docker.internal")

            env.setdefault("PREFECT_ORION_HOST", api_url)

        if prefect.settings.orion.database.connection_url:
            db_url = (
                prefect.settings.orion.database.connection_url.get_secret_value()
                .replace("localhost", "host.docker.internal")
                .replace("127.0.0.1", "host.docker.internal")
            )

            env.setdefault("PREFECT_ORION_DATABASE_CONNECTION_URL", db_url)

        db_url = env.get("PREFECT_ORION_DATABASE_CONNECTION_URL")
        if (not db_url or "sqlite" in db_url) and "PREFECT_ORION_HOST" not in env:
            warnings.warn(
                "A standalone server has not been configured and the database "
                "connection url is unconfigured or using SQLite. It is likely that "
                "your flow run container will not be able to contact the API."
            )

        return env

    def _get_labels(self, flow_run: FlowRun):
        labels = self.labels.copy() if self.labels else {}
        labels.update(
            {
                "io.prefect.flow-run-id": str(flow_run.id),
            }
        )
        return labels
