"""Create private professional spaces; existing Kadrian/Neona history stays in place."""
import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent_workspaces import PROFILES, scaffold


def prepare(root, repository_root):
    root = Path(root).resolve()
    locations = {}
    for role in PROFILES:
        if role.startswith("neona-"):
            target = root / "channels" / role.removeprefix("neona-")
        else:
            target = root / "agents" / role
        locations[role] = scaffold(target, role, repository_root)
    checks = root / "context" / "CHECKS.md"
    if not checks.exists():
        checks.parent.mkdir(parents=True, exist_ok=True)
        with checks.open("x", encoding="utf-8") as stream:
            stream.write("# Проверки\n\nРезультаты проверок сохраняются с источником и областью проекта.\n")
    return locations


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True, help="Existing Neona project root")
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[1]
    for role, folder in prepare(args.root, repo).items():
        print(role + " → " + str(folder))
