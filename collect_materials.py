import argparse
import ctypes
from datetime import datetime
from pathlib import Path
import time

from PIL import ImageChops, ImageStat

from game_window import capture_game, find_game_window


def main():
    parser = argparse.ArgumentParser(description="保存前台明日方舟客户端的校准截图")
    parser.add_argument("--minutes", type=int, default=10)
    args = parser.parse_args()
    if args.minutes < 1:
        parser.error("--minutes 至少为 1")
    ctypes.windll.user32.SetProcessDPIAware()
    directory = Path(__file__).resolve().parent / "materials" / datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    deadline = time.monotonic() + args.minutes * 60
    previous = None
    count = 0
    state = None
    try:
        while time.monotonic() < deadline:
            game = find_game_window()
            image = capture_game(game[0]) if game else None
            current_state = "采集中" if image else ("等待游戏切到前台" if game else "等待明日方舟 PC 客户端")
            if current_state != state:
                print(current_state, flush=True)
                state = current_state
            if image:
                sample = image.convert("RGB").resize((160, 90))
                if previous is None or max(ImageStat.Stat(ImageChops.difference(sample, previous)).mean) >= 0.8:
                    directory.mkdir(parents=True, exist_ok=True)
                    count += 1
                    image.save(directory / f"{count:04d}.jpg", quality=94, subsampling=0)
                    previous = sample
                    print(f"saved {count:04d}", flush=True)
            time.sleep(1.5)
    except KeyboardInterrupt:
        pass
    print(f"采集结束，共 {count} 张" + (f" · {directory}" if count else ""), flush=True)


if __name__ == "__main__":
    main()
