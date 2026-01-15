import io
import json
import os
import sys
import urllib.request
import zipfile
from pathlib import Path


CHROME_VERSION = "143.0.7499.193"
META_URL = "https://googlechromelabs.github.io/chrome-for-testing/known-good-versions-with-downloads.json"
OUTPUT_DIR = Path("C:/chromeDriver")


def main() -> None:
    data = json.load(urllib.request.urlopen(META_URL))
    match = next((v for v in data["versions"] if v["version"] == CHROME_VERSION), None)
    if not match:
        match = next((v for v in data["versions"] if v["version"].startswith("143.0.7499.")), None)
    if not match:
        print("No matching ChromeDriver version found.")
        sys.exit(1)

    download = next((d for d in match["downloads"]["chromedriver"] if d["platform"] == "win64"), None)
    if not download:
        print("No win64 ChromeDriver build found.")
        sys.exit(1)

    print(f"Downloading {download['url']}")
    response = urllib.request.urlopen(download["url"])
    zf = zipfile.ZipFile(io.BytesIO(response.read()))
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    name = next(n for n in zf.namelist() if n.endswith("chromedriver.exe"))
    zf.extract(name, OUTPUT_DIR)
    src = OUTPUT_DIR / name
    dst = OUTPUT_DIR / "chromedriver.exe"
    if dst.exists():
        dst.unlink()
    os.replace(src, dst)

    extracted_dir = OUTPUT_DIR / name.split("/")[0]
    if extracted_dir.is_dir():
        for root, dirs, files in os.walk(extracted_dir, topdown=False):
            for file in files:
                (Path(root) / file).unlink(missing_ok=True)
            for d in dirs:
                (Path(root) / d).rmdir()
        extracted_dir.rmdir()

    print(r"Installed to C:\chromeDriver\chromedriver.exe")


if __name__ == "__main__":
    main()
