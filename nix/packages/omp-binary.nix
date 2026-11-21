{
  stdenv,
  fetchurl,
  autoPatchelfHook,
  makeWrapper,
  lib,
  zlib,
  openssl,
}:
let
  version = "18.12.6";
in
stdenv.mkDerivation {
  pname = "oh-my-pi";
  inherit version;

  src = fetchurl {
    url = "https://github.com/can1357/oh-my-pi/releases/download/v18.12.6/omp-linux-x64";
    hash = "sha256-hdna86Bfx26M1CWuGQTrV2r40hzIX89GkHQmJuB8z6Q=";
  };

  dontUnpack = true;
  # Bun --compile stores its application payload in the executable. Preserve
  # that payload by disabling strip; still patch the ELF interpreter for NixOS.
  dontStrip = true;
  nativeBuildInputs = [ autoPatchelfHook makeWrapper ];
  buildInputs = [ stdenv.cc.cc.lib zlib openssl ];

  installPhase = ''
    runHook preInstall
    install -Dm755 "$src" "$out/bin/omp"
    runHook postInstall
  '';

  postFixup = ''
    wrapProgram "$out/bin/omp" \
      --prefix LD_LIBRARY_PATH : "${lib.makeLibraryPath [ stdenv.cc.cc.lib zlib openssl ]}"
  '';

  doInstallCheck = true;
  installCheckPhase = ''
    version_output="$($out/bin/omp --version)"
    test "$version_output" = "omp/${version}"
  '';

  meta = {
    description = "Oh My Pi coding agent (upstream release binary)";
    homepage = "https://omp.sh";
    license = lib.licenses.mit;
    mainProgram = "omp";
    platforms = [ "x86_64-linux" ];
  };
}
