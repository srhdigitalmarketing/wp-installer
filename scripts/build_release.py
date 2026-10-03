"""Build a deterministic release ZIP and download checksums without dependencies."""
import hashlib
from pathlib import Path
import shutil
import sys
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from wpi import __version__


def main():
    root = Path(__file__).resolve().parents[1]
    target = root / 'dist'
    target.mkdir(exist_ok=True)
    bundle = target / f'wp-installer-v{__version__}.zip'
    with zipfile.ZipFile(bundle, 'w', zipfile.ZIP_DEFLATED) as archive:
        sources = sorted((root / 'wpi').glob('*.py')) + [root / 'README.md', root / 'LICENSE']
        for source in sources:
            info = zipfile.ZipInfo(source.relative_to(root).as_posix(), (2026, 10, 4, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, source.read_bytes().replace(b'\r\n', b'\n'))
    shutil.copyfile(root / 'install.sh', target / 'install.sh')
    for asset in (bundle, target / 'install.sh'):
        digest = hashlib.sha256(asset.read_bytes()).hexdigest()
        Path(str(asset) + '.sha256').write_text(digest + '  ' + asset.name + '\n', encoding='utf-8', newline='\n')
        print(asset.name + ': ' + digest)


if __name__ == '__main__':
    main()
