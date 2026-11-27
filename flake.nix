{
  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs =
    {
      self,
      nixpkgs,
      flake-utils,
    }:
    flake-utils.lib.eachDefaultSystem (
      system:
      let
        pkgs = nixpkgs.legacyPackages.${system};

        pythonEnv = pkgs.home-assistant.python3Packages.python.withPackages (ps: [
          (ps.toPythonModule pkgs.home-assistant)
          ps.pytest
          ps.pytest-asyncio
        ]);

        fmtTools = with pkgs; [
          treefmt
          nixfmt
          taplo
          ruff
          python3Packages.mdformat
          prettier
          ruff
        ];
      in
      {
        devShells.default = pkgs.mkShell {
          name = "tts-relay-dev";
          packages = [
            pythonEnv
            pkgs.ffmpeg-headless
          ]
          ++ fmtTools;
        };

        checks = {
          tests =
            pkgs.runCommand "tts-relay-tests"
              {
                nativeBuildInputs = [
                  pythonEnv
                  pkgs.ffmpeg-headless
                ];
                src = self;
              }
              ''
                cp -r $src/. ./work
                chmod -R u+w ./work
                cd ./work
                export HOME="$TMPDIR"
                # Home Assistant's own deprecation warnings aren't ours to fix.
                ${pkgs.lib.getExe' pythonEnv "pytest"} tests/ -v -p no:cacheprovider -o filterwarnings=
                touch $out
              '';

          lint =
            pkgs.runCommand "tts-relay-lint"
              {
                nativeBuildInputs = [ pkgs.ruff ];
                src = self;
              }
              ''
                cp -r $src/. ./work
                chmod -R u+w ./work
                cd ./work
                export RUFF_CACHE_DIR="$TMPDIR/ruff-cache"
                ${pkgs.lib.getExe pkgs.ruff} check .
                ${pkgs.lib.getExe pkgs.ruff} format --check .
                touch $out
              '';

          fmt =
            pkgs.runCommand "tts-relay-fmt"
              {
                nativeBuildInputs = fmtTools;
                src = self;
              }
              ''
                cp -r $src/. ./work
                chmod -R u+w ./work
                cd ./work
                export HOME="$TMPDIR"
                treefmt --ci --no-cache
                touch $out
              '';
        };

        formatter = pkgs.writeShellApplication {
          name = "treefmt";
          runtimeInputs = fmtTools;
          text = ''exec treefmt "$@"'';
        };
      }
    );
}
