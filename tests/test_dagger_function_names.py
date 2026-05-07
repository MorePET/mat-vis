"""Regression gate for the Dagger CLI function-name contract (#240).

Dagger's Python SDK converts a snake_case ``@function`` method name to
camelCase before exposing it on the GraphQL/engine surface. The Go-side
``dagger call`` CLI then converts that camelCase to kebab-case, splitting
on **both** lowercase→uppercase and lowercase→digit boundaries. So
``derive_ktx2`` (Python) → ``deriveKtx2`` (GQL) → ``derive-ktx-2`` (CLI),
**not** ``derive-ktx2``.

#240: the ``derive.yml`` workflow used to invoke ``dagger call
derive-ktx2`` and failed in CI with ``unknown command``. This test pins
the kebab name our workflows must use, so any future @function rename
that breaks the contract trips a unit test instead of a CI dispatch.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

DAGGER_MAIN = Path(__file__).resolve().parent.parent / ".dagger" / "src" / "mat_vis_ci" / "main.py"


def _camel_to_kebab(camel: str) -> str:
    """Replicate dagger's Go-side camelCase → kebab-case conversion.

    Inserts a hyphen before any uppercase letter or digit that follows
    a lowercase letter, then lowercases the whole thing. Empirically
    matches `dagger functions` output on engine v0.20.x for both
    letter→letter (``probeSources`` → ``probe-sources``) and
    letter→digit (``deriveKtx2`` → ``derive-ktx-2``,
    ``testE2E`` → ``test-e-2-e``) boundaries.
    """
    # Engine treats every letter↔digit and lower→upper transition as a
    # word boundary. Empirically (engine v0.20.x) ``testE2E`` splits on
    # all four edges → ``test-e-2-e``. Apply each rule in sequence on
    # the running result so chained transitions all fire.
    s = camel
    s = re.sub(r"([a-z])([A-Z])", r"\1-\2", s)  # lower → upper
    s = re.sub(r"([a-zA-Z])([0-9])", r"\1-\2", s)  # letter → digit
    s = re.sub(r"([0-9])([a-zA-Z])", r"\1-\2", s)  # digit → letter
    return s.lower()


def _snake_to_camel(snake: str) -> str:
    head, *tail = snake.split("_")
    return head + "".join(p.title() for p in tail)


def _snake_to_kebab(snake: str) -> str:
    return _camel_to_kebab(_snake_to_camel(snake))


@pytest.fixture(scope="module")
def function_python_names() -> list[str]:
    """Parse @function-decorated method names out of .dagger/.../main.py.

    Lightweight regex scan — avoids importing the dagger SDK (heavy
    runtime + GraphQL engine deps) just to introspect names.
    """
    src = DAGGER_MAIN.read_text()
    # Match @function (with or without args) immediately followed by an
    # async def or def line.
    pattern = re.compile(
        r"@function(?:\([^)]*\))?\s*\n\s*(?:async\s+)?def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(",
        re.MULTILINE,
    )
    names = pattern.findall(src)
    assert names, f"no @function-decorated methods found in {DAGGER_MAIN}"
    return names


class TestKebabConversion:
    """Pure-function tests for the kebab conversion helper."""

    @pytest.mark.parametrize(
        ("snake", "expected_kebab"),
        [
            # Simple, no digits
            ("derive", "derive"),
            ("probe_sources", "probe-sources"),
            ("build_materialx", "build-materialx"),
            # #240: letter→digit boundary splits
            ("derive_ktx2", "derive-ktx-2"),
            ("test_e2e", "test-e-2-e"),
            # Hypothetical future drift cases
            ("foo_512", "foo-512"),  # snake-cased digit segment
            ("foo_bar2", "foo-bar-2"),
            ("foo2_bar", "foo-2-bar"),
        ],
    )
    def test_snake_to_kebab(self, snake: str, expected_kebab: str) -> None:
        assert _snake_to_kebab(snake) == expected_kebab


class TestDaggerFunctionNames:
    """Pin the kebab CLI name our workflows invoke for each @function.

    If a future refactor renames ``derive_ktx2`` (and forgets to update
    ``derive.yml``), this test fails in unit tests — long before a CI
    dispatch hits the same ``unknown command`` error #240 chased.
    """

    def test_derive_ktx2_is_exposed_as_kebab_with_split_digit(
        self, function_python_names: list[str]
    ) -> None:
        assert "derive_ktx2" in function_python_names, (
            "derive_ktx2 @function disappeared from .dagger/src/mat_vis_ci/main.py — "
            "if it was renamed, update .github/workflows/derive.yml line 14 + 210"
        )
        assert _snake_to_kebab("derive_ktx2") == "derive-ktx-2"

    def test_test_e2e_is_exposed_as_kebab_with_split_digit(
        self, function_python_names: list[str]
    ) -> None:
        assert "test_e2e" in function_python_names
        assert _snake_to_kebab("test_e2e") == "test-e-2-e"

    def test_validate_prod_preflight_is_exposed_as_kebab(
        self, function_python_names: list[str]
    ) -> None:
        """mat-vis#345: bake.yml's preflight job invokes
        `dagger call validate-prod-preflight`. Pin the kebab name so a
        future rename trips a unit test instead of breaking the prod
        gate at dispatch time.
        """
        assert "validate_prod_preflight" in function_python_names, (
            "validate_prod_preflight @function disappeared from "
            ".dagger/src/mat_vis_ci/main.py — if it was renamed, update "
            "bake.yml's `preflight` job step (mat-vis#345)"
        )
        assert _snake_to_kebab("validate_prod_preflight") == "validate-prod-preflight"

    def test_workflow_bake_yml_uses_correct_kebab_name_for_preflight(self) -> None:
        bake_yml = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "bake.yml"
        text = bake_yml.read_text()
        assert "validate-prod-preflight" in text, (
            "bake.yml's preflight job must invoke `validate-prod-preflight` (mat-vis#345)"
        )

    def test_validate_release_is_exposed_as_kebab(self, function_python_names: list[str]) -> None:
        """mat-vis#273: workflows invoke `dagger call validate-release`.

        Regular boundary (no digit), but pin it explicitly so a future
        rename can't silently break bake.yml + release-validate.yml.
        """
        assert "validate_release" in function_python_names, (
            "validate_release @function disappeared from .dagger/src/mat_vis_ci/main.py — "
            "if it was renamed, update bake.yml's post-bake validate step + "
            "release-validate.yml's validate step (mat-vis#273)"
        )
        assert _snake_to_kebab("validate_release") == "validate-release"

    def test_workflow_release_validate_yml_uses_correct_kebab_name(self) -> None:
        rv_yml = (
            Path(__file__).resolve().parent.parent
            / ".github"
            / "workflows"
            / "release-validate.yml"
        )
        text = rv_yml.read_text()
        assert "validate-release" in text, (
            "release-validate.yml must invoke `validate-release` Dagger function (mat-vis#273)"
        )

    def test_workflow_bake_yml_uses_correct_kebab_name_for_validate(self) -> None:
        bake_yml = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "bake.yml"
        text = bake_yml.read_text()
        assert "validate-release" in text, (
            "bake.yml's post-bake validate step must invoke `validate-release` (mat-vis#273)"
        )

    def test_workflow_derive_yml_uses_correct_kebab_name(self) -> None:
        """The ternary in derive.yml must match the kebab name above."""
        derive_yml = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "derive.yml"
        text = derive_yml.read_text()
        assert "'derive-ktx-2'" in text, (
            "derive.yml ternary must invoke 'derive-ktx-2' (kebab with split "
            "digit, per #240). Found:\n"
            + "\n".join(line for line in text.splitlines() if "derive-ktx" in line)
        )
        assert "'derive-ktx2'" not in text, (
            "derive.yml still references the broken 'derive-ktx2' literal — "
            "update the ternary AND the header comment (#240)."
        )

    def test_workflow_e2e_yml_uses_correct_kebab_name(self) -> None:
        e2e_yml = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "e2e.yml"
        text = e2e_yml.read_text()
        # `args:` block must invoke `test-e-2-e`. Header comment may
        # also include the kebab form for documentation.
        assert "test-e-2-e" in text, (
            "e2e.yml must invoke `test-e-2-e` (kebab with split digit, #240)"
        )
        # The literal broken form should not appear as a CLI invocation.
        # (Allow it inside a code reference like `test_e2e` Python name.)
        assert re.search(r"\btest-e2e\b", text) is None, (
            "e2e.yml still references the broken `test-e2e` literal (#240)."
        )

    def test_all_decorated_functions_have_unique_kebab_names(
        self, function_python_names: list[str]
    ) -> None:
        """Sanity check: no two @functions collide on their CLI name."""
        kebabs = [_snake_to_kebab(n) for n in function_python_names]
        dupes = {k for k in kebabs if kebabs.count(k) > 1}
        assert not dupes, f"duplicate kebab CLI names across @functions: {dupes}"
