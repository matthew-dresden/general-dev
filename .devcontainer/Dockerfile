# syntax=docker/dockerfile:1
FROM mcr.microsoft.com/devcontainers/base:noble

RUN mkdir -p /home/vscode/.vscode-server/bin /home/vscode/.vscode-server/extensionsCache \
  && chown -R vscode:vscode /home/vscode/.vscode-server

# Install the AWS session-manager-plugin at build time (AC-FUNC-003), so the
# tool ships inside the immutable image rather than being added by
# postCreateCommand. No trusted registry Feature installs this plugin
# (operator ruling, work unit E5-F3-S1-T2, signed commit
# 9a38c703adda210e27c2c34607cb1a09a4d63378): this RUN step is the sanctioned,
# one-time exception to the no-shell-in-build-logic rule for exactly this
# step. The package is fetched over TLS and its detached signature is
# verified against AWS's published signing key (fingerprint 7959 6371 24CE
# 093A D501 D47A 2C4D 4AFF 6F67 57EE, key ID 2C4D4AFF6F6757EE,
# `devcontainer_config.plugin_install` pins the same values) before dpkg ever
# installs it; any verification failure aborts the build (AC-FUNC-002). No
# checksum is pinned: the download always targets AWS's "latest" path, and
# pinning a digest for a moving target would break every future rebuild the
# moment AWS ships a new version at that URL.
RUN <<'INSTALL_SESSION_MANAGER_PLUGIN'
#!/usr/bin/env bash
set -euo pipefail
arch="$(dpkg --print-architecture)"
case "$arch" in
  amd64) plugin_dir="ubuntu_64bit" ;;
  arm64) plugin_dir="ubuntu_arm64" ;;
  *)
    echo "ERROR: session-manager-plugin has no known package for dpkg architecture '$arch'" >&2
    exit 1
    ;;
esac
package_url="https://s3.amazonaws.com/session-manager-downloads/plugin/latest/${plugin_dir}/session-manager-plugin.deb"
signature_url="${package_url}.sig"
workdir="$(mktemp -d)"
curl --fail --show-error --silent --tlsv1.2 --output "${workdir}/session-manager-plugin.deb" "$package_url"
curl --fail --show-error --silent --tlsv1.2 --output "${workdir}/session-manager-plugin.deb.sig" "$signature_url"
export GNUPGHOME="${workdir}/gnupg"
mkdir -m 700 "$GNUPGHOME"
cat > "${workdir}/session-manager-plugin-signer.gpg" <<'PUBLIC_KEY'
-----BEGIN PGP PUBLIC KEY BLOCK-----

mFIEZ5ERQxMIKoZIzj0DAQcCAwQjuZy+IjFoYg57sLTGhF3aZLBaGpzB+gY6j7Ix
P7NqbpXyjVj8a+dy79gSd64OEaMxUb7vw/jug+CfRXwVGRMNtIBBV1MgU1NNIFNl
c3Npb24gTWFuYWdlciA8c2Vzc2lvbi1tYW5hZ2VyLXBsdWdpbi1zaWduZXJAYW1h
em9uLmNvbT4gKEFXUyBTeXN0ZW1zIE1hbmFnZXIgU2Vzc2lvbiBNYW5hZ2VyIFBs
dWdpbiBMaW51eCBTaWduZXIgS2V5KYkBAAQQEwgAqAUCZ5ERQ4EcQVdTIFNTTSBT
ZXNzaW9uIE1hbmFnZXIgPHNlc3Npb24tbWFuYWdlci1wbHVnaW4tc2lnbmVyQGFt
YXpvbi5jb20+IChBV1MgU3lzdGVtcyBNYW5hZ2VyIFNlc3Npb24gTWFuYWdlciBQ
bHVnaW4gTGludXggU2lnbmVyIEtleSkWIQR5WWNxJM4JOtUB1HosTUr/b2dX7gIe
AwIbAwIVCAAKCRAsTUr/b2dX7rO1AQCa1kig3lQ78W/QHGU76uHx3XAyv0tfpE9U
oQBCIwFLSgEA3PDHt3lZ+s6m9JLGJsy+Cp5ZFzpiF6RgluR/2gA861M=
=2DQm
-----END PGP PUBLIC KEY BLOCK-----
PUBLIC_KEY
gpg --batch --import "${workdir}/session-manager-plugin-signer.gpg"
fingerprint="$(gpg --batch --with-colons --fingerprint 2C4D4AFF6F6757EE | awk -F: '/^fpr:/ { print $10; exit }')"
if [ "$fingerprint" != "7959637124CE093AD501D47A2C4D4AFF6F6757EE" ]; then
  echo "ERROR: session-manager-plugin signing key fingerprint mismatch: got '$fingerprint'" >&2
  exit 1
fi
gpg --batch --verify "${workdir}/session-manager-plugin.deb.sig" "${workdir}/session-manager-plugin.deb"
dpkg --install "${workdir}/session-manager-plugin.deb"
find "${workdir}" -mindepth 1 -delete
rmdir "${workdir}"
INSTALL_SESSION_MANAGER_PLUGIN
