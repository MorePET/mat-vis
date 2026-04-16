"""mat-vis CI pipeline.

Usage:
    dagger call build           # build baker image from Containerfile
    dagger call lint            # ruff check
    dagger call test            # pytest
    dagger call smoke           # verify imports inside baker image
    dagger call test-all        # lint + test + build + smoke
    dagger call push            # build + push to GHCR
"""

from typing import Annotated

import dagger
from dagger import Doc, dag, function, object_type

IMAGE = "ghcr.io/morepet/mat-vis-baker"
TARGET_PLATFORM = dagger.Platform("linux/amd64")


@object_type
class MatVisCi:
    """CI pipeline for mat-vis baker container."""

    @function
    def build(
        self,
        src: Annotated[dagger.Directory, Doc("Project root directory")] | None = None,
    ) -> dagger.Container:
        """Build the baker container image from Containerfile (always linux/amd64)."""
        context = src or dag.host().directory(".")
        return context.docker_build(dockerfile="Containerfile", platform=TARGET_PLATFORM)

    @function
    async def lint(
        self,
        src: Annotated[dagger.Directory, Doc("Project root directory")] | None = None,
    ) -> str:
        """Run ruff check on src/ and tests/."""
        context = src or dag.host().directory(".")
        return await (
            dag.container()
            .from_("python:3.12-slim")
            .with_exec(["pip", "install", "--quiet", "ruff>=0.4"])
            .with_mounted_directory("/app", context)
            .with_workdir("/app")
            .with_exec(["ruff", "check", "src/", "tests/"])
            .stdout()
        )

    @function
    async def test(
        self,
        src: Annotated[dagger.Directory, Doc("Project root directory")] | None = None,
    ) -> str:
        """Run pytest on the test suite."""
        context = src or dag.host().directory(".")
        pip_cache = dag.cache_volume("pip-cache")
        return await (
            dag.container()
            .from_("python:3.12-slim")
            .with_mounted_cache("/root/.cache/pip", pip_cache)
            .with_mounted_directory("/app", context)
            .with_workdir("/app")
            .with_exec(["pip", "install", "--quiet", "-e", ".[baker,dev]"])
            .with_exec(["pytest", "tests/", "-v"])
            .stdout()
        )

    @function
    async def smoke(
        self,
        src: Annotated[dagger.Directory, Doc("Project root directory")] | None = None,
    ) -> str:
        """Build baker image and verify pyarrow + MaterialX import."""
        ctr = self.build(src)
        return await ctr.with_exec(
            ["python", "-c", "import pyarrow; import MaterialX; print('ok')"]
        ).stdout()

    @function
    async def test_all(
        self,
        src: Annotated[dagger.Directory, Doc("Project root directory")] | None = None,
    ) -> str:
        """Run full CI: lint + test + container build + smoke test."""
        context = src or dag.host().directory(".")

        # Run lint and test in parallel (they're independent)
        lint_result = self.lint(context)
        test_result = self.test(context)
        smoke_result = self.smoke(context)

        lint_out = await lint_result
        test_out = await test_result
        smoke_out = await smoke_result

        return f"=== lint ===\n{lint_out}\n=== test ===\n{test_out}\n=== smoke ===\n{smoke_out}"

    @function
    async def push(
        self,
        src: Annotated[dagger.Directory, Doc("Project root directory")] | None = None,
        registry_user: Annotated[str, Doc("GHCR username")] = "",
        registry_pass: Annotated[dagger.Secret | None, Doc("GHCR token")] = None,
        tag: Annotated[str, Doc("Image tag")] = "latest",
    ) -> str:
        """Build and push baker image to GHCR."""
        ctr = self.build(src)

        if registry_pass is not None:
            ctr = ctr.with_registry_auth("ghcr.io", registry_user, registry_pass)

        ref = f"{IMAGE}:{tag}"
        return await ctr.publish(ref)
