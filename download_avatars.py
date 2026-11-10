"""Download operator portraits referenced by pact_recommendations.json from PRTS."""

from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
from pathlib import Path
import subprocess
import time
from urllib.parse import quote


ROOT = Path(__file__).resolve().parent
DESTINATION = ROOT / "assets" / "avatars"
DATA = json.loads((ROOT / "pact_recommendations.json").read_text(encoding="utf-8"))
SUPPLEMENTAL = json.loads((ROOT / "supplemental_pacts.json").read_text(encoding="utf-8"))
OPERATORS = sorted({name for pact in DATA for name in pact["operators"]}
                   | {name for members in SUPPLEMENTAL.values() for name in members})


def download(name):
    filename = f"头像_{name}.png"
    digest = hashlib.md5(filename.replace(" ", "_").encode("utf-8")).hexdigest()
    encoded = quote(filename)
    url = (f"https://media.prts.wiki/thumb/{digest[0]}/{digest[:2]}/{encoded}/"
           f"64px-{encoded}")
    output = DESTINATION / f"{hashlib.sha1(name.encode('utf-8')).hexdigest()}.png"
    if output.exists() and output.stat().st_size:
        return name, "cached"
    for attempt in range(3):
        result = subprocess.run(
            ["curl.exe", "-L", "--fail", "--silent", "--show-error", "--max-time", "30",
             url, "--output", str(output)],
            capture_output=True, text=True, timeout=40,
        )
        if result.returncode == 0:
            data = output.read_bytes()
            if data.startswith(b"\x89PNG\r\n\x1a\n"):
                return name, "downloaded"
            output.unlink(missing_ok=True)
            return name, "invalid PNG response"
        output.unlink(missing_ok=True)
        if attempt < 2:
            time.sleep(1.5 * (attempt + 1))
    return name, result.stderr.strip() or f"curl exit {result.returncode}"


def main():
    DESTINATION.mkdir(parents=True, exist_ok=True)
    failures = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(download, name) for name in OPERATORS]
        for future in as_completed(futures):
            name, result = future.result()
            print(f"{result}: {name}")
            if result not in ("cached", "downloaded"):
                failures.append(name)
    print(f"{len(OPERATORS) - len(failures)}/{len(OPERATORS)} portraits available in {DESTINATION}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
