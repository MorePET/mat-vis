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
      in
      {
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
              ];

              enterShell = ''
                echo "mat-vis dev shell — Python, uv, dagger, podman, ruff"
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
