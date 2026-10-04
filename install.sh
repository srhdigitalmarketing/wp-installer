#!/usr/bin/env bash
# WPI application bootstrap. Website/server packages are installed by wpi itself.
set -Eeuo pipefail
umask 022

WPI_VERSION="v1.2.1"
REPOSITORY="srhdigitalmarketing/wp-installer"
BUNDLE=""
EXPECTED_SHA=""
CHECK_ONLY=0
WORK=""
STAGE=""
LAUNCHER_TEMP=""

usage() {
    cat <<'HELP'
WPI bootstrap untuk Ubuntu 22.04 / 24.04 LTS

  sudo bash install.sh [--version v1.2.1] [--repo OWNER/REPO]
  sudo bash install.sh --bundle ./wp-installer-v1.2.1.zip --sha256 SHA256
  bash install.sh --bundle ./wp-installer-v1.2.1.zip --sha256 SHA256 --check-only

Pilihan:
  --version TAG   Release GitHub yang dipasang (default: v1.2.1).
  --repo REPO     Repository GitHub publik OWNER/REPO.
  --bundle FILE   Gunakan ZIP lokal tanpa mengunduh bundle.
  --sha256 HASH   SHA256 tepercaya; jika kosong, gunakan manifest release.
  --check-only    Verifikasi ZIP dan sintaks Python, tanpa pemasangan/paket apt.
                 Memerlukan ZIP lokal dan Python 3.10 atau lebih baru.
  -h, --help     Tampilkan bantuan.

Untuk bundle lokal tanpa --sha256, simpan manifest <nama-bundle>.sha256
di sebelah ZIP. Installer aplikasi tidak mengubah situs atau database.
HELP
}

fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
cleanup() {
    [[ -z "$WORK" ]] || rm -rf -- "$WORK"
    [[ -z "$STAGE" ]] || rm -rf -- "$STAGE"
    [[ -z "$LAUNCHER_TEMP" ]] || rm -f -- "$LAUNCHER_TEMP"
}
trap cleanup EXIT

while (($#)); do
    case "$1" in
        --version|--repo|--bundle|--sha256)
            (($# >= 2)) || fail "Nilai untuk $1 belum diisi."
            case "$1" in
                --version) WPI_VERSION="$2" ;;
                --repo) REPOSITORY="$2" ;;
                --bundle) BUNDLE="$2" ;;
                --sha256) EXPECTED_SHA="${2,,}" ;;
            esac
            shift 2
            ;;
        --check-only) CHECK_ONLY=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) fail "Pilihan tidak dikenal: $1" ;;
    esac
done

[[ "$WPI_VERSION" =~ ^v[0-9]+\.[0-9]+\.[0-9]+(-[A-Za-z0-9.-]+)?$ ]] || fail "Tag versi tidak valid."
[[ "$REPOSITORY" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || fail "Repository harus berupa OWNER/REPO."
[[ -z "$EXPECTED_SHA" || "$EXPECTED_SHA" =~ ^[0-9a-f]{64}$ ]] || fail "SHA256 harus 64 digit hex."

if ((CHECK_ONLY)); then
    [[ -n "$BUNDLE" ]] || fail "--check-only memerlukan --bundle lokal."
    command -v python3 >/dev/null || fail "Python 3.10+ dibutuhkan untuk validasi lokal."
else
    [[ "$EUID" -eq 0 ]] || fail "Jalankan dengan sudo atau sebagai root."
    [[ -r /etc/os-release ]] || fail "Sistem ini tidak memiliki /etc/os-release."
    # Source only in a subshell: os-release defines VERSION and other generic names.
    WPI_OS_INFO="$(
        # shellcheck source=/dev/null
        . /etc/os-release
        printf '%s\n%s' "${ID:-}" "${VERSION_ID:-}"
    )"
    WPI_OS_ID="${WPI_OS_INFO%%$'\n'*}"
    WPI_OS_VERSION="${WPI_OS_INFO#*$'\n'}"
    [[ "$WPI_OS_ID" == "ubuntu" ]] || fail "Hanya Ubuntu yang didukung."
    [[ "$WPI_OS_VERSION" == "22.04" || "$WPI_OS_VERSION" == "24.04" ]] || fail "Gunakan Ubuntu 22.04 atau 24.04 LTS."
    command -v apt-get >/dev/null || fail "apt-get tidak ditemukan."
    missing=()
    command -v python3 >/dev/null || missing+=(python3)
    command -v curl >/dev/null || missing+=(curl)
    command -v unzip >/dev/null || missing+=(unzip)
    [[ -s /etc/ssl/certs/ca-certificates.crt ]] || missing+=(ca-certificates)
    if ((${#missing[@]})); then
        apt-get update
        DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "${missing[@]}"
    fi
fi

python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' || fail "Python 3.10+ dibutuhkan."
WORK="$(mktemp -d -t wpi-bootstrap.XXXXXXXX)"
ASSET="wp-installer-${WPI_VERSION}.zip"
MANIFEST=""
if [[ -n "$BUNDLE" ]]; then
    [[ -f "$BUNDLE" && ! -L "$BUNDLE" ]] || fail "Bundle lokal harus file ZIP biasa."
    if [[ -z "$EXPECTED_SHA" ]]; then
        MANIFEST="${BUNDLE}.sha256"
        [[ -f "$MANIFEST" ]] || fail "Manifest $MANIFEST tidak ada; gunakan --sha256."
    fi
else
    BASE_URL="https://github.com/${REPOSITORY}/releases/download/${WPI_VERSION}"
    BUNDLE="$WORK/$ASSET"
    printf 'Mengunduh WPI %s dari %s...\n' "$WPI_VERSION" "$REPOSITORY"
    curl --fail --show-error --silent --location --proto '=https' --proto-redir '=https' --tlsv1.2 --retry 3 \
        --connect-timeout 20 --max-time 300 \
        "$BASE_URL/$ASSET" --output "$BUNDLE"
    if [[ -z "$EXPECTED_SHA" ]]; then
        MANIFEST="$WORK/$ASSET.sha256"
        curl --fail --show-error --silent --location --proto '=https' --proto-redir '=https' --tlsv1.2 --retry 3 \
            --connect-timeout 20 --max-time 300 \
            "$BASE_URL/$ASSET.sha256" --output "$MANIFEST"
    fi
fi

if [[ -z "$EXPECTED_SHA" ]]; then
    EXPECTED_SHA="$(python3 - "$MANIFEST" "$BUNDLE" <<'PY'
import pathlib, re, sys
manifest, bundle = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
matches = []
for line in manifest.read_text(encoding="utf-8").splitlines():
    found = re.fullmatch(r"([0-9a-fA-F]{64}) [ *](.+)", line)
    if found and found[2] == bundle.name:
        matches.append(found[1].lower())
if len(matches) != 1:
    sys.exit("Manifest harus memuat tepat satu SHA256 untuk " + bundle.name)
print(matches[0])
PY
)"
fi

python3 - "$BUNDLE" "$EXPECTED_SHA" "$WORK/extracted" <<'PY'
import hashlib, pathlib, py_compile, re, stat, sys, zipfile
bundle, expected, output = pathlib.Path(sys.argv[1]), sys.argv[2], pathlib.Path(sys.argv[3])
digest = hashlib.sha256()
with bundle.open("rb") as source:
    for chunk in iter(lambda: source.read(1024 * 1024), b""):
        digest.update(chunk)
if digest.hexdigest() != expected:
    sys.exit("SHA256 bundle tidak cocok; pemasangan dibatalkan.")
with zipfile.ZipFile(bundle) as archive:
    entries = archive.infolist()
    if not entries or len(entries) > 5000 or sum(item.file_size for item in entries) > 64 * 1024 * 1024:
        sys.exit("Bundle kosong atau melampaui batas ukuran.")
    names = set()
    normalized_names = set()
    for item in entries:
        path = pathlib.PurePosixPath(item.filename)
        mode = item.external_attr >> 16
        normalized = path.as_posix()
        if (not item.filename or "\\" in item.filename or ":" in item.filename
                or path.is_absolute() or ".." in path.parts or item.filename in names
                or normalized in normalized_names
                or item.filename != normalized + ("/" if item.is_dir() else "")
                or stat.S_ISLNK(mode) or (stat.S_IFMT(mode) and not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)))):
            sys.exit("Path ZIP tidak aman: " + item.filename)
        names.add(item.filename)
        normalized_names.add(normalized)
    candidates = ["", *sorted({item.filename.split("/", 1)[0] + "/" for item in entries if "/" in item.filename})]
    valid = [prefix for prefix in candidates if all(prefix + "wpi/" + name in names
             for name in ("__init__.py", "cli.py", "core.py", "web.py"))]
    if len(valid) != 1:
        sys.exit("Bundle harus memuat tepat satu paket wpi lengkap.")
    prefix = valid[0]
    output.mkdir(mode=0o755)
    for item in entries:
        if not item.filename.startswith(prefix + "wpi/") or item.is_dir():
            continue
        relative = pathlib.PurePosixPath(item.filename[len(prefix):])
        target = output.joinpath(*relative.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(archive.read(item))
        target.chmod(0o644)
    for name in ("README.md", "LICENSE"):
        source_name = prefix + name
        if source_name in names:
            (output / name).write_bytes(archive.read(source_name))
    for path in (output / "wpi").rglob("*.py"):
        py_compile.compile(str(path), doraise=True)
    print("SHA256 dan sintaks paket Python valid.")
PY

if ((CHECK_ONLY)); then
    printf 'Validasi selesai. Tidak ada aplikasi atau paket sistem yang dipasang.\n'
    exit 0
fi

APP="/usr/local/lib/wpi"
RELEASES="/usr/local/lib/wpi-releases"
COMMAND="/usr/local/bin/wpi"
if [[ -e "$APP" || -L "$APP" ]]; then
    [[ -L "$APP" ]] || fail "$APP sudah ada dan bukan symlink WPI; tidak ditimpa."
    case "$(readlink -f -- "$APP")" in
        "$RELEASES"/*) ;;
        *) fail "$APP menunjuk aplikasi lain; tidak ditimpa." ;;
    esac
fi
if [[ -e "$COMMAND" || -L "$COMMAND" ]]; then
    [[ -f "$COMMAND" && ! -L "$COMMAND" ]] || fail "$COMMAND bukan launcher WPI biasa."
    [[ "$(head -n 2 -- "$COMMAND" | tail -n 1)" == "# WPI managed launcher" ]] || fail "$COMMAND sudah digunakan aplikasi lain."
fi
for path in "$RELEASES" /var/lib/wpi /var/lib/wpi/sites /var/backups/wpi /var/log/wpi; do
    [[ ! -L "$path" ]] || fail "Direktori $path tidak boleh symlink."
    [[ ! -e "$path" || -d "$path" ]] || fail "$path bukan direktori."
done
install -d -m 0755 /usr/local/lib /usr/local/bin "$RELEASES"
install -d -m 0700 /var/lib/wpi /var/lib/wpi/sites /var/backups/wpi
install -d -m 0750 /var/log/wpi
STAGE="$(mktemp -d "$RELEASES/${WPI_VERSION}.XXXXXXXX")"
cp -a -- "$WORK/extracted/." "$STAGE/"
printf '%s\n' "$WPI_VERSION" > "$STAGE/VERSION"
chmod 0755 "$STAGE"
LAUNCHER_TEMP="$(mktemp /usr/local/bin/.wpi-launcher.XXXXXXXX)"
cat > "$LAUNCHER_TEMP" <<'LAUNCHER'
#!/usr/bin/env bash
# WPI managed launcher
set -euo pipefail
cd /usr/local/lib/wpi
exec env -u PYTHONPATH -u PYTHONHOME /usr/bin/python3 -m wpi.cli "$@"
LAUNCHER
chmod 0755 "$LAUNCHER_TEMP"
NEXT="$RELEASES/.link-$(basename -- "$STAGE")"
ln -s -- "$STAGE" "$NEXT"
mv -Tf -- "$NEXT" "$APP"
STAGE="" # Installed releases remain available for rollback.
mv -Tf -- "$LAUNCHER_TEMP" "$COMMAND"
LAUNCHER_TEMP=""
# Upgrade an existing managed server automatically; fresh servers activate this
# during their first setup. Older bundles remain usable for application rollback.
if [[ -f "$APP/wpi/autotune.py" && -f /var/lib/wpi/config.json ]]; then
    "$COMMAND" autotune-enable
fi
printf '\nWPI %s berhasil dipasang. Jalankan: sudo wpi\n' "$WPI_VERSION"
printf 'Data situs dipertahankan di /var/lib/wpi; backup di /var/backups/wpi.\n'
