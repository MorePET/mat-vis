{
  description = "mat-vis — PBR texture mirror, baker + data pipeline";

  nixConfig = {
    extra-trusted-public-keys = "devenv.cachix.org-1:w1cLUi8dv3hnoSPGAuibQv+f9TZLr6cv/Hm9XgU50cw=";
    extra-substituters = "https://devenv.cachix.org";
  };

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
    devenv.url = "github:cachix/devenv";
    devenv.inputs.nixpkgs.follows = "nixpkgs";
    dagger.url = "github:dagger/nix";
    dagger.inputs.nixpkgs.follows = "nixpkgs";
  };

  outputs =
    {
      self,
      nixpkgs,
      flake-utils,
      devenv,
      dagger,
    }@inputs:
    flake-utils.lib.eachDefaultSystem (
      system:
      let
        pkgs = nixpkgs.legacyPackages.${system};

        # ── nix-native baker toolchain (hybrid POC) ───────────────────
        # nixpkgs materialx 1.39.4 ships the FULL render module
        # (PyMaterialXRender.BaseType + PyMaterialXRenderGlsl.TextureBaker)
        # and imports cleanly — no manylinux-wheel / LD_LIBRARY_PATH hack.
        # This is the reproducible, rootless (no engine, no bridge network)
        # tier: what `nix flake check` runs locally == what CI runs.
        pyEnv = pkgs.python3.withPackages (
          ps: with ps; [
            materialx
            pillow
            pytest
            requests
            pyarrow
            huggingface-hub
            pyyaml
          ]
        );

        # A pytest check derivation. Copies the tracked flake source (no
        # .venv/.git — flakes only see git-tracked files) into a writable
        # build dir and runs `testPath`. The sandbox has NO DISPLAY, so the
        # GLX-bound bake tests skip cleanly; the display-free MaterialX
        # API-contract tests (guarding the #442/#443/#445 signature+import
        # regressions) run for real. Headless GLX stays in Dagger's
        # `test-materialx` (Debian libgl1 wires mesa GLX; a bare nix X
        # server does not — the one thing the container does more easily).
        pytestCheck =
          name: testPath: extraArgs:
          pkgs.runCommand name { nativeBuildInputs = [ pyEnv ]; } ''
            export HOME=$TMPDIR PYTHONDONTWRITEBYTECODE=1
            cp -r ${./.} work && chmod -R u+w work && cd work
            # repo root on PYTHONPATH so tests importing `scripts.*` resolve
            export PYTHONPATH=$PWD/src:$PWD/clients/python/src:$PWD
            python -m pytest ${testPath} -v -p no:cacheprovider ${extraArgs} \
              --basetemp="$TMPDIR/pt" 2>&1 | tee "$out"
          '';
      in
      {
        # `nix flake check` runs these; also `nix build .#checks.<sys>.<name>`.
        checks = {
          materialx-smoke = pytestCheck "materialx-smoke" "tests/test_bake_mtlx_smoke.py" "";
          # test_client_runtime_version_matches_pyproject reads the client
          # version via importlib.metadata, which only resolves for an
          # INSTALLED distribution. This check runs from the source tree, so
          # that invariant belongs to the Dagger/pip `test` job (which does
          # `pip install -e ./clients/python`), not here.
          unit = pytestCheck "unit" "tests/" (
            "--deselect tests/test_version_sync.py::test_client_runtime_version_matches_pyproject"
          );
        };

        # Reproducible OCI baker base, built entirely by Nix (cache.nixos.org,
        # no image pulls / podman bridge). `nix build .#baker-base` produces a
        # tarball that `podman load < result` accepts ROOTLESS — the
        # community-standard "Nix packages, podman runs" path (skopeo/
        # containers-storage is podman's native stack). Feed this to Dagger's
        # `_baker_container` via `from_()` to kill the unpinned-apt base and
        # pin the toolchain.
        packages.baker-base = pkgs.dockerTools.buildLayeredImage {
          name = "mat-vis-baker-base";
          tag = "nix";
          contents = [
            pyEnv
            pkgs.xvfb-run
            pkgs.xvfb
            pkgs.mesa
            pkgs.bashInteractive
            pkgs.coreutils
          ];
          config = {
            Env = [
              "LIBGL_ALWAYS_SOFTWARE=1"
              "PYTHONDONTWRITEBYTECODE=1"
            ];
            Entrypoint = [ "${pyEnv}/bin/python" ];
          };
        };

        devShells.default = devenv.lib.mkShell {
          inherit inputs pkgs;
          modules = [
            {
              languages.python = {
                enable = true;
                uv.enable = true;
                uv.sync.enable = true;
              };

              packages = [
                dagger.packages.${system}.dagger
                pkgs.git
                pkgs.gh
                pkgs.podman
                pkgs.ruff
                # headless-render toolchain, so the MaterialX smoke test can
                # be attempted locally (`nix build .#checks.<sys>.materialx-smoke`
                # is the canonical reproducible runner).
                pkgs.xvfb-run
                pkgs.mesa
                pkgs.skopeo # load nix-built images into rootless podman
              ];

              enterShell = ''
                echo "mat-vis dev shell — Python, uv, dagger, podman, ruff, xvfb, skopeo"
              '';

              git-hooks.hooks = {
                ruff.enable = true;
                ruff-format.enable = true;
                nixfmt.enable = true;
              };
            }
          ];
        };
      }
    );
}
