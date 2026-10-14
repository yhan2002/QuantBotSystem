"""Copy credential-free templates locally without replacing existing settings."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSIONS = ("QuantBotSystem", "QuantBotSystem_Beta")


def prepare(root=ROOT):
    for version in VERSIONS:
        folder = root / version
        content = (folder / "config.local.example.py").read_bytes()
        target = folder / "config.py"
        try:
            with target.open("xb") as stream:
                stream.write(content)
        except FileExistsError:
            print(f"Preserved existing {version}/config.py")
        else:
            print(f"Created ignored {version}/config.py from the blank template")


if __name__ == "__main__":
    prepare()
