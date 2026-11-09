import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

if __name__ == "__main__":
    subprocess.run([shutil.which("pnpm") or "pnpm", "generate"], cwd=ROOT, check=True)
