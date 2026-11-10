import base64
import ctypes
from ctypes import wintypes
from datetime import datetime
import hashlib
import io
import json
import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import threading
from time import monotonic
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk
from urllib.parse import quote
from urllib.request import urlopen

from PIL import Image, ImageChops, ImageFilter, ImageOps, ImageTk

from game_window import BackgroundCapture, WindowThumbnail, capture_game, client_size, find_game_window, foreground_client_box


HERE = Path(__file__).resolve().parent
DATA_DIR = (
    Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    / "StrongholdProtocolAssistant"
    if getattr(sys, "frozen", False) else HERE
)
PACT_DATA = json.loads((HERE / "pact_recommendations.json").read_text(encoding="utf-8"))
STRATEGY_PACTS = {name: group["pact"] for group in PACT_DATA for name in group["strategies"]}
CORE_PACTS = {group["pact"]: tuple(group["operators"]) for group in PACT_DATA}
SUPPLEMENTAL_PACTS = {
    name: tuple(members)
    for name, members in json.loads((HERE / "supplemental_pacts.json").read_text(encoding="utf-8")).items()
}
PACT_OPERATORS = {**CORE_PACTS, **SUPPLEMENTAL_PACTS}
PACT_MEMBERS = {name for members in PACT_OPERATORS.values() for name in members}
OPERATOR_NAMES = frozenset(json.loads((HERE / "operator_names.json").read_text(encoding="utf-8"))) | PACT_MEMBERS
OPERATOR_TRAITS = json.loads((HERE / "operator_traits.json").read_text(encoding="utf-8"))
EQUIPMENT_TIERS = json.loads((HERE / "equipment_tiers.json").read_text(encoding="utf-8"))
EQUIPMENT_DETAILS = json.loads((HERE / "equipment_details.json").read_text(encoding="utf-8"))
EQUIPMENT_RANK = {name: tier for tier, names in EQUIPMENT_TIERS.items() for name in names}
# PRTS 卫戍协议·规程 → 模拟配置 → 参战干员一览（默认阵容）。
OPERATOR_TIERS = {
    name: int(tier)
    for tier, names in json.loads((HERE / "operator_tiers.json").read_text(encoding="utf-8")).items()
    for name in names.split()
}


DIFFICULTIES = ("入门协议", "标准模拟", "险境模拟", "绝境模拟", "终极模拟")
CARD_NAME_BOXES = tuple((left, .929, left + .085, .962) for left in (.441, .554, .668, .782))
CARD_NAME_LEFTS = (.102, .215, .328, .441, .554, .668, .782)
READY_BOX = (.875, .117, 1, .175)
DEPLOYMENT_BOX = (.87, .56, 1, .62)
HEALTH_BOX = (.60, .02, .65, .07)
ENTRY_PHASES = {"卫戍协议大厅", "模拟难度选择", "搜寻队友中"}
MATCH_PHASES = {"确认本局信息", "策略选择", "确定自选编队", "局内筹备", "干员详情", "装备详情"}


def normalize(value):
    return re.sub(r"\s+", "", value)


def load_presets(path, *, allow_empty=False):
    return parse_presets(json.loads(path.read_text(encoding="utf-8-sig")), allow_empty=allow_empty)


def parse_presets(data, *, allow_empty=False):
    if (not isinstance(data, dict) or not isinstance(data.get("presets"), list)
            or (not allow_empty and not data["presets"])):
        raise ValueError("文件需要包含非空 presets 数组")
    presets = {}
    for preset in data["presets"]:
        if not isinstance(preset, dict):
            raise ValueError("每套阵容都需要填写 name 和 operators")
        name = preset.get("name")
        operators = preset.get("operators")
        if not isinstance(name, str) or not name.strip() or name.strip() in presets:
            raise ValueError("阵容名称不能为空或重复")
        if not isinstance(operators, list) or not operators or any(
            not isinstance(operator, str) or not normalize(operator) for operator in operators
        ):
            raise ValueError(f"{name} 的 operators 需要填写非空干员名称")
        operators = tuple(normalize(operator) for operator in operators)
        if len(set(operators)) != len(operators):
            raise ValueError(f"{name} 中有重复干员")
        presets[name.strip()] = operators
    return presets


def recognize_names(lines, vocabulary):
    if not vocabulary:
        return set()
    pattern = "|".join(re.escape(name) for name in sorted(vocabulary, key=lambda name: (-len(name), name)))
    return {name for line in lines for name in re.findall(pattern, normalize(line))}


def recommend(targets, candidates, owned):
    remaining = [name for name in targets if name not in owned]
    return [name for name in remaining if name in candidates], remaining


def track_strategy(preview, readings):
    phase = readings.get("phase")
    state = readings.get("strategy_state")
    if state == "预览待确认":
        # An unreadable new preview may be a different card.
        return readings.get("previewed_strategy"), None
    if state == "等待队友选择" or phase in ("确定自选编队", "局内筹备"):
        return None, preview
    if state == "轮到自己选择" or phase in ("卫戍协议大厅", "模拟难度选择", "搜寻队友中", "确认本局信息"):
        return None, None
    return preview, None


def session_state_after(state, phase):
    if phase == "游戏主界面":
        return "idle"
    if phase == "卫戍协议大厅":
        return "between" if state in ("match", "between") else "entry"
    if phase in MATCH_PHASES:
        return "match"
    if phase in ENTRY_PHASES and state != "match":
        return "entry"
    return state


def text_in_region(lines, box):
    left, top, right, bottom = box
    result = []
    for line in lines:
        words = [
            word["text"] for word in line["words"]
            if left <= word["x"] + word["w"] / 2 <= right
            and top <= word["y"] + word["h"] / 2 <= bottom
        ]
        if words:
            result.append(" ".join(words))
    return result


def analyze_frame(lines, regions, vocabulary, details=None):
    details = details or {}
    labels = [normalize(line["text"]) for line in lines]
    result = {"phase": "未判定", "ocr": [line["text"] for line in lines]}
    if any("请选择模拟难度" in label for label in text_in_region_normalized(lines, (.78, 0, .96, .09))):
        result["phase"] = "模拟难度选择"
        # The lower-left summary and highlighted row both show the current selection.
        selected_text = (
            text_in_region_normalized(lines, (0, .93, .09, 1))
            + text_in_region_normalized(lines, (.80, .42, .94, .50))
        )
        matches = {name for name in DIFFICULTIES if any(name in label for label in selected_text)}
        if len(matches) == 1:
            result["selected_difficulty"] = matches.pop()
        search_text = text_in_region_normalized(lines, (.63, .92, .77, .98))
        for mode in ("快速搜寻", "精确搜寻"):
            if mode in search_text:
                result["search_mode"] = mode
                break
        return result
    if any("协议数据库" in label for label in labels) and any("物资调配处" in label for label in labels):
        result["phase"] = "卫戍协议大厅"
        return result
    if sum(any(anchor in label for label in labels) for anchor in ("公开招募", "干员寻访", "采购中心", "基建")) >= 3:
        result["phase"] = "游戏主界面"
        return result

    searching = text_in_region_normalized(lines, (.65, .25, .97, .42))
    if any("搜寻中" in label for label in searching):
        result["phase"] = "搜寻队友中"
        record_difficulty(result, searching)
        for mode in ("快速搜寻", "精确搜寻"):
            if any(mode in label for label in searching):
                result["search_mode"] = mode
                break
        return result

    header = "".join(text_in_region_normalized(lines, (.28, 0, .56, .09)))
    if re.search(r"确认本?局信息|1/3确认.{0,1}局信息", header):
        result["phase"] = "确认本局信息"
        record_difficulty(result, text_in_region_normalized(lines, (0, .01, .17, .085)))
        enemy_panel = text_in_region_normalized(lines, (.70, .42, .94, .75))
        for label in enemy_panel:
            leader = re.search(r"假想敌[:：](.+)", label)
            if leader:
                result["enemy_leader"] = leader[1]
        modifiers = [label.removeprefix("特训敌人").lstrip("·，、:：.") for label in enemy_panel
                     if label.startswith("特训敌人")]
        if modifiers:
            result["enemy_modifiers"] = modifiers
        return result

    if "确定自选编队" in header:
        result["phase"] = "确定自选编队"
        record_difficulty(result, text_in_region_normalized(lines, (0, .01, .17, .085)))
        return result

    if "选择策略" in header or re.search(r"2/3选择.略", header) or any(
        label in ("选择策略", "策略选择") for label in labels
    ):
        result["phase"] = "策略选择"
        record_difficulty(result, text_in_region_normalized(lines, (0, .01, .17, .085)))
        prompt = "".join(text_in_region_normalized(lines, (.72, .02, .87, .112)))
        if re.search(r"请选择.略", prompt):
            result["strategy_state"] = "预览待确认" if shows_strategy_preview(lines) else "轮到自己选择"
            if details.get("previewed_strategy") and result["strategy_state"] == "预览待确认":
                result["previewed_strategy"] = details["previewed_strategy"]
        elif re.search(r"正在选择.略", prompt):
            result["strategy_state"] = "等待队友选择"
        return result

    if details.get("phase") in ("干员详情", "装备详情"):
        result.update(details)
        return result

    if details.get("phase") == "局内筹备" or shows_preparation(lines):
        result["phase"] = "局内筹备"
        result.update(details)
        hud = "".join(text_in_region_normalized(lines, HEALTH_BOX))
        health = re.search(r"\d{1,3}$", hud)
        if health:
            result["health"] = int(health[0])
        if "shop" not in details:
            cards = [read_card_name(lines, box) for box in CARD_NAME_BOXES]
            result.update(shop_readings(cards, [box[0] for box in CARD_NAME_BOXES]))
        result["candidate_source"] = "自动定位 · 底部卡片"
        if "shop" in regions:
            result["shop"] = text_in_region(lines, regions["shop"])
            result["candidate_source"] = "手动框选"
            result.pop("operator_slots", None)
        if "team" in regions:
            result["team"] = text_in_region(lines, regions["team"])
        return result

    anchors = [line["words"][0] for line, label in zip(lines, labels)
               if line["words"] and re.fullmatch(r"调度中心(?:[Ll][Vv]\.?\d+)?", label)]
    if not anchors and "shop" not in regions:
        return result

    if anchors:
        result["phase"] = "调度中心"
    if "shop" in regions:
        result["shop"] = text_in_region(lines, regions["shop"])
        result["candidate_source"] = "手动框选"
    elif len(anchors) == 1:
        x, y = anchors[0]["x"], anchors[0]["y"]
        area = (max(0, x - .42), y + .018, min(1, x + .72), min(1, y + .38))
        matches = [line for line in text_in_region(lines, area)
                   if recognize_names([line], vocabulary)]
        if matches:
            result["shop"] = matches
            result["candidate_source"] = "自动定位（试验）"
    if "team" in regions:
        result["team"] = text_in_region(lines, regions["team"])
    return result


def text_in_region_normalized(lines, box):
    return [normalize(text) for text in text_in_region(lines, box)]


def record_difficulty(result, labels):
    matches = {name for name in DIFFICULTIES if any(name in label for label in labels)}
    if len(matches) == 1:
        result["difficulty"] = matches.pop()


def shows_strategy_preview(lines):
    return any("确认选择" in label for label in text_in_region_normalized(lines, (.79, .83, .98, .92)))


def shows_preparation(lines):
    ready = "".join(text_in_region_normalized(lines, (.87, .11, 1, .20)))
    deployment = "".join(text_in_region_normalized(lines, DEPLOYMENT_BOX))
    return "准备就绪" in ready and "剩余可放置角色" in deployment


def read_card_name(lines, box):
    for text in text_in_region_normalized(lines, box):
        match = re.fullmatch(r"[\u3400-\u9fff·]{1,12}", text)
        if match:
            return match[0]
    return None


def crop_relative(image, box):
    return image.crop(tuple(round(value * image.size[index % 2]) for index, value in enumerate(box)))


def scan_region_text(ocr, image, box):
    return [normalize(line["text"]) for line in ocr.scan(crop_relative(image, box))]


def read_expanded_shop(ocr, image, lines, slots, frozen=False):
    cards = []
    lefts = CARD_NAME_LEFTS[-slots:]
    for left in lefts:
        box = (left, .928, left + .088, .961)
        if left == lefts[0]:
            texts = text_in_region_normalized(lines, box)
            name = next((text for text in texts if text in EQUIPMENT_DETAILS), None)
            if not name:
                crop = crop_relative(image, box).convert("RGB")
                for channel in (crop.convert("L"), crop.getchannel("R")):
                    texts = [normalize(line["text"]) for line in ocr.scan(ImageOps.autocontrast(channel))]
                    name = next((text for text in texts if text in EQUIPMENT_DETAILS), None)
                    if name:
                        break
            cards.append(name or read_card_name(lines, box) or next(iter(texts), None))
            continue
        name = read_card_name(lines, box)
        if frozen and name not in OPERATOR_NAMES and left != lefts[0]:
            crop = crop_relative(image, (left, .930, left + .061, .959)).convert("RGB")
            for channel in (crop.convert("L"), crop.getchannel("R")):
                names = (normalize(line["text"]).strip("[]【】「」")
                         for line in ocr.scan(ImageOps.autocontrast(channel)))
                match = next((candidate for candidate in names if candidate in OPERATOR_NAMES), None)
                if match:
                    name = match
                    break
        if not name:
            texts = scan_region_text(ocr, image, (left, .926, left + .082, .963))
            names = [text.strip("[]【】「」") for text in texts]
            name = next((name for name in names if re.fullmatch(r"[\u3400-\u9fff·]{1,12}", name)), None)
        cards.append(name or read_card_name(lines, box))
    return shop_readings(cards, lefts)


def shop_readings(cards, lefts):
    # The name starts just left of the center of each card.
    slots = [{"name": name, "x": round(left + .03, 3)}
             for name, left in zip(cards[1:], lefts[1:]) if name]
    return {"equipment_candidates": cards[:1] if cards[0] else [],
            "equipment_slots": [{"name": cards[0], "x": round(lefts[0] + .03, 3)}] if cards[0] else [],
            "shop": [slot["name"] for slot in slots], "operator_slots": slots}


def read_preparation_hud(ocr, image):
    result = {}
    round_box = crop_relative(image, (807/2048, 38/1152, 835/2048, 59/1152))
    digit = round_box.convert("L").point(lambda value: 255 if value < 100 else 0)
    bounds = digit.getbbox()
    if bounds:
        left, top, right, bottom = bounds
        width, height = right - left, bottom - top
        if height >= digit.height * .75 and .30 <= width / height <= .46:
            result["round"] = 1
        else:
            sample = ImageOps.invert(digit).resize((90, 75))
            repeated = Image.new("L", (350, 125), 255)
            for x in (25, 130, 235):
                repeated.paste(sample, (x, 25))
            values = re.findall(r"\d+", " ".join(line["text"] for line in ocr.scan(repeated)))
            if len(values) >= 2 and len(set(values)) == 1:
                result["round"] = int(values[0])

    timer_box = (1825/2048, 54/1152, 1960/2048, 139/1152)
    screen = crop_relative(image, timer_box)
    alternate = screen.resize(tuple(max(1, round(side * (0.5 if image.height > 800 else 2)))
                                    for side in screen.size))
    # Resample once more for the slanted digits when both native scales fail.
    canonical = screen.resize((95, 60), Image.Resampling.LANCZOS)
    for crop in (screen, alternate, canonical):
        timer = read_timer(ocr, crop)
        if timer is not None:
            result["countdown_seconds"] = timer
            break
    return result


def read_timer(ocr, crop):
    screen = crop.convert("RGB").rotate(18, expand=True).resize((550, 350)).crop((140, 110, 440, 232))
    red, green, blue = screen.split()
    mask = ImageChops.darker(ImageChops.subtract(blue, red).point(lambda value: 255 if value > 35 else 0),
                            ImageChops.subtract(blue, green).point(lambda value: 255 if value > 25 else 0))
    for width in (225, 300):
        sample = ImageOps.expand(ImageOps.invert(mask).resize((width, mask.height)), border=25, fill=255)
        for line in ocr.scan(sample):
            value = normalize(line["text"])
            if re.fullmatch(r"(?:\d{1,2}|S\d)", value):
                return int(value.replace("S", "5"))
    return None


def scan_frame(ocr, image, hud=None, refresh_hud=True):
    lines = ocr.scan(image)
    preparation = shows_preparation(lines)
    if image.height <= 800 and not preparation:
        enlarged = image.resize((image.width * 2, image.height * 2), Image.Resampling.LANCZOS)
        enlarged_lines = ocr.scan(enlarged)
        if shows_preparation(enlarged_lines):
            lines = enlarged_lines
            preparation = True
    if not preparation:
        ready = scan_region_text(ocr, image, READY_BOX)
        if any("准备就绪" in text for text in ready):
            preparation = any("剩余可放置角色" in text for text in scan_region_text(ocr, image, DEPLOYMENT_BOX))
            if not preparation:
                action = "".join(scan_region_text(ocr, image, (.875, .89, 1, .945)))
                kind = "干员" if "至此售出" in action else "装备" if "至此销毁" in action else None
                if kind:
                    title_box = (0, .273, .17, .319)
                    titles = text_in_region_normalized(lines, title_box) + scan_region_text(ocr, image, title_box)
                    name = next((title for title in titles if re.fullmatch(r"[\u3400-\u9fff·]{2,12}", title)), None)
                    return lines, {"phase": f"{kind}详情", "selected_name": name}
    if preparation:
        details = {"phase": "局内筹备"}
        frozen = any("解冻" in text for text in text_in_region_normalized(lines, (.77, .62, .89, .71)))
        # Shop upgrades add slots on the left. Empty slots retain their positions.
        layouts = [(slots, (left, .90, left + .063, .942))
                   for slots, left in ((5, .222), (6, .108), (7, 0))]
        for slots, box in layouts:
            if any("升级" in text for text in text_in_region_normalized(lines, box)):
                details.update(read_expanded_shop(ocr, image, lines, slots, frozen))
                break
        else:
            for slots, box in layouts:
                crop = crop_relative(image, box)
                boosted = ImageOps.autocontrast(crop.convert("L"))
                if any("升级" in normalize(line["text"]) for line in ocr.scan(boosted)):
                    details.update(read_expanded_shop(ocr, image, lines, slots, frozen))
                    break
        if refresh_hud:
            hud = read_preparation_hud(ocr, image)
            health = re.search(r"\d{1,3}$", "".join(scan_region_text(ocr, image, HEALTH_BOX)))
            if health:
                hud["health"] = int(health[0])
        details.update(hud or {})
        return lines, details
    details = {}
    if shows_strategy_preview(lines):
        title = crop_relative(image, (.79, .505, .90, .553))
        for line in ocr.scan(title):
            name = re.fullmatch(r"[\u3400-\u9fff·]{2,12}", normalize(line["text"]).strip("[]【】〖〗"))
            if name:
                details["previewed_strategy"] = name[0]
                break
    return lines, details


class WindowsOCR:
    def __enter__(self):
        command = "& ([scriptblock]::Create([IO.File]::ReadAllText((Join-Path (Get-Location) 'ocr.ps1'))))"
        self.process = subprocess.Popen(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
            cwd=HERE, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            encoding="utf-8", creationflags=subprocess.CREATE_NO_WINDOW,
        )
        try:
            self.max_dimension = self.receive()["max"]
        except Exception:
            self.__exit__()
            raise
        return self

    def __exit__(self, *_):
        self.process.terminate()
        self.process.communicate()

    def receive(self):
        line = self.process.stdout.readline()
        if not line:
            raise RuntimeError("Windows OCR 未能启动或已退出")
        result = json.loads(line)
        if result.get("error") == "missing_zh_ocr":
            raise RuntimeError("请在 Windows 设置中安装简体中文语言的光学字符识别功能")
        if result.get("error"):
            raise RuntimeError(result["error"])
        return result

    def scan(self, image):
        width, height = image.size
        scale = min(2 if height < 350 else 1, self.max_dimension / max(width, height))
        if scale != 1:
            image = image.resize((max(1, int(width * scale)), max(1, int(height * scale))), Image.Resampling.LANCZOS)
        buffer = io.BytesIO()
        image.save(buffer, format="PNG", compress_level=1)
        self.process.stdin.write(base64.b64encode(buffer.getvalue()).decode("ascii") + "\n")
        self.process.stdin.flush()
        lines = self.receive()["lines"]
        for line in lines:
            for word in line["words"]:
                for coordinate, size in (("x", image.width), ("w", image.width),
                                         ("y", image.height), ("h", image.height)):
                    word[coordinate] /= size
        return lines


def scan_loop(hwnd, stop, output):
    try:
        with WindowsOCR() as ocr, BackgroundCapture(hwnd) as background:
            paused = False
            missed_frames = 0
            hud, next_hud = {}, 0
            while not stop.is_set():
                if not ctypes.windll.user32.IsWindow(hwnd):
                    raise RuntimeError("游戏窗口已关闭")
                image = capture_game(hwnd, background)
                if image is None:
                    missed_frames += 1
                    if not paused and missed_frames >= 4:
                        output.put(("paused", None))
                        paused = True
                    stop.wait(0.15)
                    continue
                paused = False
                missed_frames = 0
                captured = datetime.now().astimezone().isoformat(timespec="milliseconds")
                refresh_hud = monotonic() >= next_hud
                lines, details = scan_frame(ocr, image, hud, refresh_hud)
                if details.get("phase") == "局内筹备":
                    if refresh_hud:
                        hud = {key: details[key] for key in ("round", "health", "countdown_seconds") if key in details}
                        next_hud = monotonic() + 2
                else:
                    hud, next_hud = {}, 0
                output.put(("frame", (captured, image, lines, details)))
                stop.wait(0.12)
    except Exception as error:
        output.put(("error", str(error)))
    finally:
        output.put(("done", None))


class RecommendationOverlay:
    def __init__(self, parent):
        transparent = "#ff00ff"
        window = tk.Toplevel(parent)
        window.overrideredirect(True)
        window.geometry("1x1")
        window.attributes("-transparentcolor", transparent)
        self.canvas = tk.Canvas(window, background=transparent, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        with Image.open(HERE / "assets" / "card_pointer_hand.png") as source:
            self.hand = source.convert("RGBA")
        alpha = self.hand.getchannel("A").point(lambda value: 255 if value >= 128 else 0)
        bounds = alpha.getbbox()
        if bounds:
            self.hand = self.hand.crop(bounds)
            alpha = alpha.crop(bounds)
        tip = alpha.crop((0, max(0, alpha.height - 4), alpha.width, alpha.height)).getbbox()
        self.tip_fraction = (tip[0] + tip[2]) / (2 * alpha.width) if tip else .5
        self.hand_photo = None
        self.hand_height = 0
        window.update_idletasks()
        self.user = ctypes.windll.user32
        self.user.GetAncestor.argtypes = (wintypes.HWND, wintypes.UINT)
        self.user.GetAncestor.restype = wintypes.HWND
        self.hwnd = self.user.GetAncestor(window.winfo_id(), 2)  # GA_ROOT
        self.user.GetWindowLongW.argtypes = (wintypes.HWND, ctypes.c_int)
        self.user.SetWindowLongW.argtypes = (wintypes.HWND, ctypes.c_int, ctypes.c_long)
        self.user.SetWindowPos.argtypes = (wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                           ctypes.c_int, ctypes.c_int, wintypes.UINT)
        self.user.ShowWindow.argtypes = (wintypes.HWND, ctypes.c_int)
        style = self.user.GetWindowLongW(self.hwnd, -20)
        # WS_EX_TRANSPARENT passes clicks through the layered window.
        # WS_EX_NOACTIVATE keeps the game in the foreground.
        self.user.SetWindowLongW(self.hwnd, -20, style | 0x20 | 0x08000000)
        self.displayed = None
        self.user.ShowWindow(self.hwnd, 0)

    def update(self, box, arrows):
        if not box or not arrows:
            if self.displayed is not None:
                self.user.ShowWindow(self.hwnd, 0)
                self.displayed = None
            return
        state = (box, arrows)
        if state == self.displayed:
            return
        self.displayed = state
        left, top, right, bottom = box
        width, height = right - left, bottom - top
        tip = round(height * .741)
        arrow_height = max(42, round(height * .097))
        padding = max(5, round(arrow_height * .12))
        if arrow_height != self.hand_height:
            hand_width = max(1, round(self.hand.width * arrow_height / self.hand.height))
            resized = self.hand.resize((hand_width, arrow_height), Image.Resampling.LANCZOS)
            # Win32 color-key transparency cannot blend partially transparent pixels.
            alpha = resized.getchannel("A").point(lambda value: 255 if value >= 128 else 0)
            silver = resized.convert("L").convert("RGBA")
            silver.putalpha(alpha)
            halo = ImageOps.expand(alpha, border=padding, fill=0)
            halo = halo.filter(ImageFilter.MaxFilter(3)).filter(ImageFilter.GaussianBlur(padding / 3))
            # Ordered dithering fades the glow without blending into the color key.
            thresholds = ((0, 8, 2, 10), (12, 4, 14, 6), (3, 11, 1, 9), (15, 7, 13, 5))
            glow_alpha = bytes(
                255 if value > 8 + 16 * thresholds[(i // halo.width) % 4][(i % halo.width) % 4] else 0
                for i, value in enumerate(halo.tobytes())
            )
            glow = Image.new("RGBA", halo.size, "#c1eeff")
            glow.putalpha(Image.frombytes("L", halo.size, glow_alpha))
            glow.alpha_composite(silver, (padding, padding))
            self.hand_photo = ImageTk.PhotoImage(glow, master=self.canvas)
            self.hand_height = arrow_height
        self.canvas.delete("all")
        tip_offset = padding + round((self.hand_photo.width() - 2 * padding) * self.tip_fraction)
        for center in arrows:
            x = round(center * width)
            self.canvas.create_image(x - tip_offset, 0, image=self.hand_photo, anchor="nw")
        # HWND_TOPMOST with SWP_NOACTIVATE | SWP_SHOWWINDOW.
        self.user.SetWindowPos(self.hwnd, -1, left, top + tip - arrow_height - padding,
                               width, self.hand_photo.height(), 0x0010 | 0x0040)


class Assistant(tk.Tk):
    def configure_theme(self):
        background = "#edf2f6"
        surface = "#ffffff"
        ink = "#172a36"
        muted = "#506775"
        border = "#d5e0e7"
        accent = "#087d70"
        font = ("Microsoft YaHei UI", 9)
        self.configure(background=background)
        self.option_add("*Font", font)
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure(".", font=font, foreground=ink, background=surface,
                        bordercolor=border, lightcolor=border, darkcolor=border)
        style.configure("Shell.TFrame", background=background)
        style.configure("TLabelFrame", background=surface, bordercolor=border, relief="solid")
        style.configure("TLabelFrame.Label", font=("Microsoft YaHei UI", 9, "bold"),
                        foreground=ink, background=surface)
        style.configure("Muted.TLabel", foreground=muted)
        style.configure("Header.TFrame", background="#172e3d")
        style.configure("Header.TLabel", foreground="#ffffff", background="#172e3d",
                        font=("Microsoft YaHei UI", 16, "bold"))
        style.configure("HeaderBadge.TLabel", foreground="#9de7d9", background="#172e3d",
                        font=("Microsoft YaHei UI", 9, "bold"))
        style.configure("Status.TLabel", foreground="#225968", background="#e8f4f6", padding=(8, 5))
        style.configure("TEntry", fieldbackground="#f8fbfc", padding=(6, 4))
        style.map("TEntry", bordercolor=[("focus", accent)])
        style.configure("TButton", padding=(7, 4), background="#f3f7fa", relief="flat")
        style.map("TButton", background=[("disabled", "#f5f7f9"), ("pressed", "#dce8ed"),
                                         ("active", "#e8f1f5")],
                  foreground=[("disabled", "#667d8a")], bordercolor=[("focus", accent)])
        style.configure("Accent.TButton", background=accent, foreground="#ffffff")
        style.map("Accent.TButton", background=[("disabled", "#e3edf0"), ("pressed", "#056258"),
                                                ("active", "#096e63")],
                  foreground=[("disabled", "#617884")])
        style.configure("Selected.TButton", background="#d9f1ec", foreground="#075d54")
        style.map("Selected.TButton", background=[("pressed", "#bce6db"), ("active", "#caede4")])
        style.configure("TNotebook", background=surface, borderwidth=1, tabmargins=(0, 0, 0, 0))
        style.configure("TNotebook.Tab", padding=(13, 5), background="#eaf0f4", foreground=muted)
        style.map("TNotebook.Tab", background=[("selected", "#d9f1ec"), ("active", "#e2f0f0")],
                  foreground=[("selected", "#075d54")])
        style.configure("Vertical.TScrollbar", background="#c7d7df", troughcolor="#f0f5f8",
                        borderwidth=0, arrowsize=12)

    def __init__(self):
        super().__init__()
        self.configure_theme()
        self.title("卫戍选调助手")
        self.iconbitmap(default=str(HERE / "assets" / "app_icon.ico"))
        screen_height = self.winfo_screenheight()
        height = min(850, max(600, screen_height - 50))
        self.geometry(f"920x{height}+16+{max(0, screen_height - height - 50)}")
        self.minsize(820, 600)
        self.presets = {}
        self.strategy_preview = None
        self.strategy_decision = None
        self.pact = None
        self.overlay = None
        self.arrow_slots = ()
        self.regions = {}
        self.readings = {}
        self.game = None
        self.preview_image = None
        self.preview_photo = None
        self.thumbnail = None
        self.preview_status = tk.StringVar(value="等待连接游戏")
        self.log = None
        self.log_path = None
        self.last_record = None
        self.output = queue.SimpleQueue()
        self.stop = threading.Event()
        self.worker = None
        self.failed_window = None
        self.session_state = "idle"
        self.closing = False
        self.status = tk.StringVar()
        self.game_status = tk.StringVar()
        self.preset_name = tk.StringVar()
        self.saved_lineup_name = tk.StringVar()
        self.lineup_draft = None
        self.saved_lineups_path = DATA_DIR / "saved_lineups.json"
        self.saved_lineups = {}
        self.saved_lineups_error = ""
        try:
            self.saved_lineups = load_presets(self.saved_lineups_path, allow_empty=True)
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as error:
            self.saved_lineups_error = str(error)
        self.owned = tk.StringVar()
        self.pact_buttons = {}
        self.tracked_operators = ()
        self.tracked_equipment = ()
        self.equipment_photos = {}
        self.operator_view = "pact"
        self.history_path = DATA_DIR / "custom_operator_history.json"
        self.operator_history = []
        try:
            history = json.loads(self.history_path.read_text(encoding="utf-8"))
            if isinstance(history, list):
                self.operator_history = list(dict.fromkeys(
                    name for name in history if isinstance(name, str) and name.strip()))
        except (OSError, ValueError):
            pass
        self.history_page = 0
        self.search_results = ()
        self.avatar_queue = queue.SimpleQueue()
        self.avatar_pending = set()
        self.avatar_failed = set()
        self.avatar_photos = {}

        header = ttk.Frame(self, style="Header.TFrame", padding=(18, 13))
        header.pack(fill="x")
        ttk.Label(header, text="卫戍选调助手", style="Header.TLabel").pack(side="left")
        ttk.Label(header, text="实时辅助", style="HeaderBadge.TLabel").pack(side="right")
        body = ttk.Frame(self, style="Shell.TFrame", padding=(14, 12, 14, 14))
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=3)
        body.columnconfigure(1, weight=2)
        body.rowconfigure(0, weight=1)
        left = ttk.LabelFrame(body, text="对局识别与推荐", padding=6)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        canvas = tk.Canvas(left, width=400, height=1, highlightthickness=0,
                           background=ttk.Style(self).lookup("TFrame", "background") or self.cget("background"))
        scrollbar = ttk.Scrollbar(left, orient="vertical", command=canvas.yview)
        scrollbar.pack(side="right", fill="y")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        panel = ttk.Frame(canvas, padding=6)
        content = canvas.create_window(0, 0, window=panel, anchor="nw")
        panel.bind("<Configure>", lambda _: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(content, width=event.width))
        wheel_delta = 0

        def scroll_left_panel(event):
            nonlocal wheel_delta
            hovered = self.winfo_containing(event.x_root, event.y_root)
            scroll_target = canvas
            while hovered is not None and hovered is not left:
                if hovered is self.equipment_canvas:
                    scroll_target = hovered
                hovered = hovered.master
            if hovered is None:
                wheel_delta = 0
                return
            wheel_delta += event.delta
            steps = int(wheel_delta / 120)
            if steps:
                scroll_target.yview_scroll(-steps, "units")
                wheel_delta -= steps * 120
            return "break"

        self.bind("<MouseWheel>", scroll_left_panel, add="+")
        sidebar = ttk.Frame(body, style="Shell.TFrame")
        sidebar.grid(row=0, column=1, sticky="nsew")
        self.build_sidebar(sidebar)
        choices = ttk.Frame(panel)
        choices.pack(fill="x", pady=(2, 6))
        self.lineup_code_entry = ttk.Entry(choices)
        self.lineup_code_entry.pack(side="left", fill="x", expand=True)
        self.lineup_code_entry.bind("<Return>", lambda _: self.import_lineup_code())
        self.import_code_button = ttk.Button(choices, text="导入阵容码", style="Accent.TButton",
                                             command=self.import_lineup_code)
        self.import_code_button.pack(side="left", padx=(6, 0))

        self.settings_toggle = ttk.Button(panel, text="▸ 识别设置", command=self.toggle_recognition_settings)
        self.settings_toggle.pack(fill="x", pady=(0, 4))
        self.recognition_settings = ttk.Frame(panel)
        controls = ttk.Frame(self.recognition_settings)
        controls.pack(fill="x")
        for kind, label in (("shop", "框选候选区"), ("team", "框选队伍（可选）")):
            button = ttk.Button(controls, text=label, command=lambda key=kind: self.calibrate(key))
            button.pack(side="left", padx=(0, 4))
        ttk.Button(controls, text="恢复自动", command=self.clear_regions).pack(side="left")

        ttk.Label(self.recognition_settings, text="已持有干员补录（用空格或逗号分隔）").pack(anchor="w", pady=(6, 2))
        ttk.Entry(self.recognition_settings, textvariable=self.owned).pack(fill="x")
        self.owned.trace_add("write", lambda *_: self.render())
        self.status_label = ttk.Label(panel, textvariable=self.status, wraplength=400, style="Status.TLabel")
        self.status_label.pack(fill="x", pady=(2, 6))
        saved_section = ttk.Frame(panel)
        saved_section.pack(fill="x", pady=(0, 6))
        self.saved_toggle = ttk.Button(saved_section, command=self.toggle_saved_lineups)
        self.saved_toggle.pack(fill="x")
        saved_panel = self.saved_grid = ttk.Frame(saved_section, padding=(2, 4, 2, 0))
        self.saved_buttons = []
        for index in range(max(20, len(self.saved_lineups))):
            button = ttk.Button(saved_panel, width=0, command=lambda slot=index: self.select_saved_lineup(slot))
            button.grid(row=index // 5, column=index % 5, sticky="ew", padx=2, pady=2)
            self.saved_buttons.append(button)
        for column in range(5):
            saved_panel.columnconfigure(column, weight=1, uniform="saved_slots")
        self.refresh_saved_buttons()
        self.lineup_panel = ttk.LabelFrame(panel, text="阵容干员", padding=6)
        self.lineup_grid = ttk.Frame(self.lineup_panel)
        self.lineup_grid.pack(fill="x")
        self.lineup_tiles = []
        lineup_actions = ttk.Frame(self.lineup_panel)
        lineup_actions.pack(anchor="e", pady=(4, 0))
        self.lineup_action = ttk.Button(lineup_actions, text="一键追踪", style="Accent.TButton",
                                        command=self.apply_lineup_action)
        self.lineup_action.pack(side="left", padx=2)
        self.rename_button = ttk.Button(lineup_actions, text="重命名", command=self.rename_lineup)
        self.edit_button = ttk.Button(lineup_actions, text="变更阵容", command=self.toggle_lineup_edit)
        self.delete_lineup_button = ttk.Button(lineup_actions, text="删除阵容", command=self.delete_lineup)
        self.export_lineup_button = ttk.Button(lineup_actions, text="复制阵容码", command=self.copy_lineup_code)
        pact_panel = ttk.LabelFrame(panel, text="选择盟约推荐阵容", padding=8)
        self.pact_panel = pact_panel
        pact_panel.pack(fill="x", pady=(0, 8))
        self.pact_tabs = ttk.Notebook(pact_panel)
        self.pact_tabs.grid(row=0, column=0, columnspan=5, sticky="ew", pady=(0, 4))
        self.core_pact_tab = ttk.Frame(self.pact_tabs, padding=4)
        self.supplemental_pact_tab = ttk.Frame(self.pact_tabs, padding=4)
        for tab, label, pacts in (
            (self.core_pact_tab, "核心盟约", CORE_PACTS),
            (self.supplemental_pact_tab, "附加盟约", SUPPLEMENTAL_PACTS),
        ):
            self.pact_tabs.add(tab, text=label)
            for column in range(5):
                tab.columnconfigure(column, weight=1)
            for index, pact in enumerate(pacts):
                button = ttk.Button(tab, text=pact, command=lambda name=pact: self.select_pact(name))
                button.grid(row=index // 5, column=index % 5, sticky="ew", padx=2, pady=2)
                self.pact_buttons[pact] = button
        self.equipment_tab = ttk.Frame(self.pact_tabs, padding=4)
        self.pact_tabs.add(self.equipment_tab, text="装备")
        self.build_equipment_list(self.equipment_tab)
        for column in range(5):
            pact_panel.columnconfigure(column, weight=1)
        self.search_button = ttk.Button(pact_panel, text="自选干员", command=self.show_operator_search)
        self.search_button.grid(row=2, column=0, sticky="ew", padx=2, pady=2)
        self.history_button = ttk.Button(pact_panel, text="自选过的干员", command=self.show_operator_history)
        self.history_button.grid(row=2, column=1, sticky="ew", padx=2, pady=2)
        self.pact_prompt = ttk.Frame(panel)
        self.pact_question = ttk.Label(self.pact_prompt, wraplength=300)
        self.pact_question.pack(side="left", fill="x", expand=True)
        ttk.Button(self.pact_prompt, text="是", width=4, command=lambda: self.choose_pact(True)).pack(side="left")
        ttk.Button(self.pact_prompt, text="否", width=4, command=lambda: self.choose_pact(False)).pack(side="left")
        self.avatar_panel = ttk.LabelFrame(panel, text="推荐干员", padding=6)
        self.avatar_panel.pack(fill="x", anchor="nw")
        self.search_bar = ttk.Frame(self.avatar_panel)
        self.search_entry = ttk.Entry(self.search_bar)
        self.search_entry.pack(side="left", fill="x", expand=True)
        self.search_entry.bind("<Return>", lambda _: self.search_operator())
        ttk.Button(self.search_bar, text="搜索", command=self.search_operator).pack(side="left", padx=(6, 0))
        self.avatar_grid = ttk.Frame(self.avatar_panel)
        self.avatar_grid.pack(fill="x")
        self.track_all_button = ttk.Button(self.avatar_panel, text="一键追踪", style="Accent.TButton",
                                           command=self.track_pact)
        self.history_nav = ttk.Frame(self.avatar_panel)
        self.history_prev = ttk.Button(self.history_nav, text="上一页", command=lambda: self.change_history_page(-1))
        self.history_prev.pack(side="left")
        self.history_next = ttk.Button(self.history_nav, text="下一页", command=lambda: self.change_history_page(1))
        self.history_next.pack(side="left")
        self.avatar_tiles = []
        self.tracked_panel = ttk.LabelFrame(panel, text="本局追踪干员", padding=6)
        self.tracked_panel.pack(fill="x", pady=(8, 0))
        self.tracked_grid = ttk.Frame(self.tracked_panel)
        self.tracked_grid.pack(fill="x")
        self.tracked_equipment_grid = ttk.Frame(self.tracked_panel)
        self.tracked_equipment_tiles = []
        tracking_actions = ttk.Frame(self.tracked_panel)
        tracking_actions.pack(anchor="e", pady=(4, 0))
        self.clear_tracking_button = ttk.Button(
            tracking_actions, text="一键取消跟踪", command=self.clear_tracking)
        self.clear_tracking_button.pack(side="left")
        self.save_lineup_button = ttk.Button(tracking_actions, text="保存阵容", style="Accent.TButton",
                                             command=self.save_lineup)
        self.save_lineup_button.pack(side="left", padx=(6, 0))
        self.tracked_tiles = []
        self.avatar_placeholder = tk.PhotoImage(master=self, width=64, height=64)
        for column in range(5):
            self.avatar_grid.columnconfigure(column, weight=1)
            self.tracked_grid.columnconfigure(column, weight=1)
            self.tracked_equipment_grid.columnconfigure(column, weight=1)
            self.lineup_grid.columnconfigure(column, weight=1)
        self.render()
        self.pact_tabs.bind("<<NotebookTabChanged>>", self.on_pact_tab_changed)
        self.after_idle(self.on_pact_tab_changed)
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.after(100, self.poll)
        self.after(0, self.refresh_game)

    def toggle_recognition_settings(self):
        if self.recognition_settings.winfo_manager():
            self.recognition_settings.pack_forget()
            self.settings_toggle.configure(text="▸ 识别设置")
        else:
            self.recognition_settings.pack(fill="x", before=self.status_label, pady=(0, 4))
            self.settings_toggle.configure(text="▾ 识别设置")

    def toggle_saved_lineups(self):
        if self.saved_grid.winfo_manager():
            self.saved_grid.pack_forget()
        else:
            self.saved_grid.pack(fill="x")
        self.refresh_saved_buttons()

    def on_pact_tab_changed(self, _event=None):
        selected = self.pact_tabs.select()
        if selected:
            self.pact_tabs.configure(height=self.nametowidget(selected).winfo_reqheight())
        self.render()

    def build_equipment_list(self, parent):
        tiers = ttk.Frame(parent)
        tiers.pack(fill="x", pady=(0, 4))
        self.equipment_tier = next(iter(EQUIPMENT_TIERS))
        self.equipment_tier_buttons = {}
        for tier in EQUIPMENT_TIERS:
            button = ttk.Button(tiers, text=f"{tier}阶", width=4,
                                command=lambda rank=tier: self.select_equipment_tier(rank))
            button.pack(side="left", fill="x", expand=True, padx=1)
            self.equipment_tier_buttons[tier] = button
        equipment = tk.Canvas(parent, height=132, highlightthickness=0, background="#ffffff")
        self.equipment_canvas = equipment
        scrollbar = ttk.Scrollbar(parent, orient="vertical", command=equipment.yview)
        scrollbar.pack(side="right", fill="y")
        equipment.configure(yscrollcommand=scrollbar.set)
        equipment.pack(side="left", fill="both", expand=True)
        self.equipment_grid = ttk.Frame(equipment)
        self.equipment_tiles = []
        content = equipment.create_window(0, 0, window=self.equipment_grid, anchor="nw")
        self.equipment_grid.bind("<Configure>", lambda _: equipment.configure(scrollregion=equipment.bbox("all")))
        equipment.bind("<Configure>", lambda event: equipment.itemconfigure(content, width=event.width))
        for column in range(5):
            self.equipment_grid.columnconfigure(column, weight=1)

    def select_equipment_tier(self, tier):
        self.equipment_tier = tier
        self.equipment_canvas.yview_moveto(0)
        self.render()

    def build_sidebar(self, sidebar):
        connection = ttk.LabelFrame(sidebar, text="连接设置", padding=10)
        connection.pack(fill="x", pady=(0, 8))
        ttk.Label(connection, text="桌面端 · 支持尝试后台截图", style="Muted.TLabel").pack(anchor="w")
        ttk.Label(connection, textvariable=self.game_status, wraplength=270,
                  style="Status.TLabel").pack(fill="x", pady=(5, 8))
        settings = ttk.Frame(connection)
        settings.pack(fill="x")
        self.detect_button = ttk.Button(settings, text="重新查找", command=self.retry_game)
        self.detect_button.pack(side="left")
        pinned = tk.BooleanVar(self, value=False)
        self.pinned = pinned
        ttk.Checkbutton(settings, text="窗口置顶", variable=pinned,
                        command=lambda: self.attributes("-topmost", pinned.get())).pack(side="right")

        preview = ttk.LabelFrame(sidebar, text="实时截图", padding=10)
        preview.pack(fill="x", pady=(0, 8))
        self.preview_canvas = tk.Canvas(preview, width=300, height=172, background="#142532", highlightthickness=0)
        self.preview_canvas.pack(fill="x")
        self.preview_canvas.bind("<Configure>", self.draw_preview)
        ttk.Label(preview, textvariable=self.preview_status, style="Muted.TLabel").pack(anchor="w", pady=(6, 0))

        split = ttk.Panedwindow(sidebar, orient="vertical")
        split.pack(fill="both", expand=True)
        details = ttk.LabelFrame(split, text="干员特性", padding=10)
        self.details_panel = details
        logs = ttk.LabelFrame(split, text="运行日志", padding=10)
        split.add(details, weight=1)
        split.add(logs, weight=1)
        self.detail_portrait = tk.Canvas(details, width=96, height=96, background="#f4f8fa",
                                         highlightthickness=0)
        self.detail_operator = None
        self.detail_photo = None
        detail_text = ttk.Frame(details)
        detail_text.pack(side="left", fill="both", expand=True)
        self.operator_details = self.build_sidebar_text(detail_text)
        self.operator_details.tag_configure("operator_name", font=("Microsoft YaHei UI", 12, "bold"))
        self.operator_details.tag_configure("trait_name", foreground="#087d70",
                                            font=("Microsoft YaHei UI", 9, "bold"))
        self.show_operator_details()
        self.runtime_log = self.build_sidebar_text(logs)
        self.log_message("助手已启动，正在查找游戏窗口。")

    def build_sidebar_text(self, parent):
        scrollbar = ttk.Scrollbar(parent)
        scrollbar.pack(side="right", fill="y")
        view = tk.Text(parent, width=30, height=5, wrap="word", padx=8, pady=7,
                                   background="#f4f8fa", foreground="#294350", relief="flat",
                                   borderwidth=0, highlightthickness=0, spacing3=3,
                                   selectbackground="#c8ece5", selectforeground="#172a36",
                                   state="disabled", yscrollcommand=scrollbar.set)
        view.pack(fill="both", expand=True)
        scrollbar.configure(command=view.yview)
        return view

    def show_operator_details(self, name=None):
        self.detail_operator = name
        self.refresh_detail_portrait()
        equipment = EQUIPMENT_DETAILS.get(name)
        self.details_panel.configure(text="装备详情" if equipment else "干员特性")
        view = self.operator_details
        view.configure(state="normal")
        view.delete("1.0", "end")
        if not name:
            view.insert("end", "单击头像查看特性，双击头像执行加入或移除。")
        elif equipment:
            view.insert("end", name + "\n", "operator_name")
            view.insert("end", f"阶数 · {EQUIPMENT_RANK[name]}\n\n")
            view.insert("end", equipment["effect"])
        else:
            view.insert("end", name + "\n", "operator_name")
            tier = OPERATOR_TIERS.get(name)
            pacts = [pact for pact, members in PACT_OPERATORS.items() if name in members]
            if tier:
                view.insert("end", f"阶数 · {'ⅠⅡⅢⅣⅤⅥ'[tier - 1]}\n")
            if pacts:
                view.insert("end", "盟约 · " + "、".join(pacts) + "\n")
            traits = OPERATOR_TRAITS.get(name, ())
            for title, description in traits:
                view.insert("end", "\n" + title + "\n", "trait_name")
                view.insert("end", description + "\n")
            if not traits:
                view.insert("end", "\n暂无本次规程的固定特质记录。")
        view.configure(state="disabled")
        view.yview_moveto(0)

    def refresh_detail_portrait(self):
        canvas = self.detail_portrait
        canvas.delete("all")
        self.detail_photo = None
        name = self.detail_operator
        if not name:
            canvas.pack_forget()
            return
        if not canvas.winfo_manager():
            canvas.pack(side="right", anchor="n", padx=(8, 0), before=self.operator_details.master)
        if name in EQUIPMENT_DETAILS:
            self.detail_photo = self.load_equipment_icon(name, size=96)
            if self.detail_photo:
                canvas.create_image(48, 48, image=self.detail_photo)
            return
        photo = self.load_avatar(name)
        if photo is None:
            return
        try:
            with Image.open(self.avatar_path(name)) as source:
                portrait = ImageOps.contain(source.convert("RGBA"), (96, 96), Image.Resampling.LANCZOS)
                self.detail_photo = ImageTk.PhotoImage(portrait, master=canvas)
        except OSError:
            self.detail_photo = photo
        canvas.create_image(48, 48, image=self.detail_photo)

    def draw_preview(self, _event=None):
        canvas = self.preview_canvas
        width, height = canvas.winfo_width(), canvas.winfo_height()
        if width <= 1 or height <= 1:
            return
        if self.game:
            source = self.game[0]
            source_width, source_height = client_size(source)
            if source_width and source_height:
                if self.thumbnail is None:
                    user = ctypes.windll.user32
                    user.GetAncestor.argtypes = (wintypes.HWND, wintypes.UINT)
                    user.GetAncestor.restype = wintypes.HWND
                    self.thumbnail = WindowThumbnail(user.GetAncestor(self.winfo_id(), 2))
                scale = min(width / source_width, height / source_height)
                w, h = round(source_width * scale), round(source_height * scale)
                x = canvas.winfo_rootx() - self.winfo_rootx() + (width - w) // 2
                y = canvas.winfo_rooty() - self.winfo_rooty() + (height - h) // 2
                if self.thumbnail.show(source, (x, y, x + w, y + h)):
                    canvas.delete("all")
                    self.preview_photo = None
                    self.preview_status.set(f"实时预览 · {source_width} × {source_height}")
                    return
        if self.thumbnail:
            self.thumbnail.close()
        canvas.delete("all")
        if self.preview_image is None:
            self.preview_photo = None
            canvas.create_text(width // 2, height // 2, text="暂无截图", fill="#d0d5dd")
            self.preview_status.set("请还原游戏窗口并切回游戏" if self.game else "等待连接游戏")
            return
        thumbnail = self.preview_image.copy()
        thumbnail.thumbnail((width, height), Image.Resampling.LANCZOS)
        self.preview_photo = ImageTk.PhotoImage(thumbnail, master=self)
        canvas.create_image(width // 2, height // 2, image=self.preview_photo)
        self.preview_status.set(f"最近一次识别截图 · {self.preview_image.width} × {self.preview_image.height}")

    def log_message(self, message, timestamp=None):
        time = timestamp[11:19] if timestamp else datetime.now().strftime("%H:%M:%S")
        view = self.runtime_log
        view.configure(state="normal")
        view.insert("end", f"[{time}] {message}\n")
        lines = int(view.index("end-1c").split(".")[0])
        if lines > 301:
            view.delete("1.0", f"{lines - 300}.0")
        view.see("end")
        view.configure(state="disabled")

    def refresh_game(self):
        if self.closing:
            return
        self.detect_game()
        self.after(1500, self.refresh_game)

    def detect_game(self):
        if self.worker is None:
            game = find_game_window()
            if game != self.game:
                self.log_message(f"已连接 · {game[1]}" if game else "游戏窗口已断开。")
            self.game = game
            if game and game[0] != self.failed_window:
                self.stop.clear()
                self.status.set("等待识别卫戍协议 · 请保持游戏窗口未最小化。")
                self.detect_button.configure(state="disabled")
                self.worker = threading.Thread(target=scan_loop, args=(game[0], self.stop, self.output), daemon=True)
                self.worker.start()
            elif not game:
                self.failed_window = None
        self.game_status.set(f"已找到 · {self.game[1]}" if self.game else "等待明日方舟 PC 客户端")
        self.draw_preview()

    def retry_game(self):
        self.failed_window = None
        self.detect_game()

    def refresh_saved_buttons(self):
        names = list(self.saved_lineups)
        marker = "▾" if self.saved_grid.winfo_manager() else "▸"
        self.saved_toggle.configure(text=f"{marker} 我的阵容 · {len(names)}/20")
        for index, button in enumerate(self.saved_buttons):
            occupied = index < len(names)
            label = names[index] if occupied else f"{index + 1:03d} · 空"
            if len(label) > 8:
                label = label[:7] + "…"
            button.configure(text=label, state="normal" if occupied and self.lineup_draft is None else "disabled")

    def import_lineup_code(self):
        if self.lineup_draft is not None:
            return
        prefix, separator, payload = normalize(self.lineup_code_entry.get()).partition("-")
        try:
            if prefix != "SP1" or not separator:
                raise ValueError("不支持的阵容码版本")
            decoded = base64.b64decode(payload + "=" * (-len(payload) % 4), altchars=b"-_", validate=True)
            presets = parse_presets(json.loads(decoded.decode("utf-8")))
            if len(presets) != 1:
                raise ValueError("阵容码只能包含一套阵容")
        except (ValueError, RecursionError):
            messagebox.showerror("导入阵容码失败", "阵容码无效，请检查是否复制完整。", parent=self)
            return
        self.presets = presets
        self.preset_name.set(next(iter(presets)))
        self.select_preset()

    def select_saved_lineup(self, slot):
        name = list(self.saved_lineups)[slot]
        self.saved_lineup_name.set(name)
        self.render()

    def select_preset(self):
        self.saved_lineup_name.set("")
        self.render()

    def save_lineup(self):
        if not self.tracked_operators:
            return
        if len(self.saved_lineups) >= 20:
            messagebox.showinfo(
                "保存阵容", "你的阵容已经存满了，保存这个的话需要先删除之前的一个哦", parent=self)
            return
        if self.saved_lineups_error:
            messagebox.showerror("保存阵容失败", self.saved_lineups_error, parent=self)
            return
        index = len(self.saved_lineups) + 1
        while f"阵容{index:03d}" in self.saved_lineups:
            index += 1
        name = f"阵容{index:03d}"
        if self.write_saved_lineups({**self.saved_lineups, name: self.tracked_operators}):
            self.render()
            self.log_message(f"已保存阵容 · {name}")

    def write_saved_lineups(self, lineups):
        data = {"presets": [{"name": key, "operators": operators} for key, operators in lineups.items()]}
        temporary = self.saved_lineups_path.with_suffix(".tmp")
        try:
            self.saved_lineups_path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(self.saved_lineups_path)
        except OSError as error:
            messagebox.showerror("保存阵容失败", str(error), parent=self)
            return False
        self.saved_lineups = lineups
        self.refresh_saved_buttons()
        return True

    def rename_lineup(self):
        old_name = self.saved_lineup_name.get()
        name = simpledialog.askstring("重命名", "输入新的阵容名称", initialvalue=old_name, parent=self)
        if name is None:
            return
        name = name.strip()
        if name == old_name:
            return
        if not name or name in self.saved_lineups:
            messagebox.showwarning("重命名", "请输入非空且未使用的阵容名称。", parent=self)
            return
        lineups = {name if key == old_name else key: members for key, members in self.saved_lineups.items()}
        if self.write_saved_lineups(lineups):
            self.saved_lineup_name.set(name)
            self.render()

    def delete_lineup(self):
        name = self.saved_lineup_name.get()
        if name not in self.saved_lineups or self.lineup_draft is not None:
            return
        if not messagebox.askyesno("删除阵容", f"确定删除「{name}」吗？", parent=self):
            return
        lineups = dict(self.saved_lineups)
        del lineups[name]
        if self.write_saved_lineups(lineups):
            self.saved_lineup_name.set("")
            self.render()

    def copy_lineup_code(self):
        name = self.saved_lineup_name.get()
        if name not in self.saved_lineups or self.lineup_draft is not None:
            return
        data = {"presets": [{"name": name, "operators": self.saved_lineups[name]}]}
        payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        code = "SP1-" + base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
        try:
            self.clipboard_clear()
            self.clipboard_append(code)
        except tk.TclError as error:
            messagebox.showerror("复制阵容码失败", str(error), parent=self)
            return
        messagebox.showinfo("复制阵容码", f"「{name}」的阵容码已复制到剪贴板。", parent=self)

    def preview_lineup(self):
        if self.lineup_draft is not None:
            return self.lineup_draft
        if self.saved_lineup_name.get():
            return self.saved_lineups.get(self.saved_lineup_name.get(), ())
        return self.presets.get(self.preset_name.get(), ())

    def toggle_lineup_edit(self):
        if self.lineup_draft is None:
            self.lineup_draft = self.preview_lineup()
        else:
            if not self.lineup_draft:
                messagebox.showwarning("变更阵容", "阵容至少需要保留一名干员。", parent=self)
                return
            lineups = {**self.saved_lineups, self.saved_lineup_name.get(): self.lineup_draft}
            if not self.write_saved_lineups(lineups):
                return
            self.lineup_draft = None
        self.refresh_saved_buttons()
        self.render()

    def apply_lineup_action(self):
        if self.lineup_draft is not None:
            self.lineup_draft = None
            self.refresh_saved_buttons()
        else:
            self.tracked_operators = tuple(dict.fromkeys((*self.tracked_operators, *self.preview_lineup())))
        self.render()

    def click_lineup_operator(self, name):
        if self.lineup_draft is None:
            self.tracked_operators = tuple(dict.fromkeys((*self.tracked_operators, name)))
        else:
            self.lineup_draft = tuple(operator for operator in self.lineup_draft if operator != name)
        self.render()

    def clear_regions(self):
        self.regions.clear()
        self.readings.clear()
        self.render()

    def calibrate(self, kind):
        if self.preview_image is None:
            self.status.set("切回游戏，等待取得候选面板截图后再框选。")
            return
        screenshot = self.preview_image
        preview = screenshot.copy()
        preview.thumbnail((min(1400, self.winfo_screenwidth() - 90),
                           min(900, self.winfo_screenheight() - 160)))
        window = tk.Toplevel(self)
        window.title("拖动框选干员名称区域 · Esc 取消")
        window.attributes("-topmost", True)
        window.resizable(False, False)
        canvas = tk.Canvas(window, width=preview.width, height=preview.height, highlightthickness=0, cursor="crosshair")
        canvas.pack()
        photo = ImageTk.PhotoImage(preview)
        canvas.create_image(0, 0, anchor="nw", image=photo)
        canvas.photo = photo
        rect = canvas.create_rectangle(0, 0, 0, 0, outline="#00ffbd", width=2)
        start = (0, 0)

        def finish():
            window.destroy()
            self.lift()

        def point(event):
            return max(0, min(preview.width, event.x)), max(0, min(preview.height, event.y))

        def press(event):
            nonlocal start
            start = point(event)
            canvas.coords(rect, *start, *start)

        def release(event):
            x, y = point(event)
            left, right = sorted((start[0], x))
            top, bottom = sorted((start[1], y))
            if right - left < 10 or bottom - top < 8:
                return
            self.regions[kind] = (
                left / preview.width, top / preview.height, right / preview.width, bottom / preview.height,
            )
            self.readings.clear()
            self.status.set("已框选。请切回游戏继续记录。")
            self.render()
            finish()

        window.protocol("WM_DELETE_WINDOW", finish)
        window.bind("<Escape>", lambda _: finish())
        window.transient(self)
        window.grab_set()
        canvas.bind("<Button-1>", press)
        canvas.bind("<B1-Motion>", lambda event: canvas.coords(rect, *start, *point(event)))
        canvas.bind("<ButtonRelease-1>", release)
        window.focus_force()

    def begin_session(self):
        self.last_record = None
        self.strategy_preview = None
        self.strategy_decision = None
        self.pact = None
        directory = DATA_DIR / "sessions"
        now = datetime.now().astimezone()
        self.log_path = directory / now.strftime("%Y%m%d-%H%M%S-%f.jsonl")
        try:
            directory.mkdir(parents=True, exist_ok=True)
            self.log = self.log_path.open("x", encoding="utf-8")
            self.write_event({"event": "start", "time": now.isoformat(), "window": self.game[1]})
        except OSError as error:
            if self.log is not None:
                self.log.close()
                self.log = None
            self.status.set(f"无法创建本局记录 · {error}")
            self.log_message(self.status.get())
            return
        self.log_message(f"检测到卫戍协议，开始记录 · {self.log_path.name}")

    def end_session(self):
        self.tracked_operators = ()
        self.tracked_equipment = ()
        if self.log is not None:
            try:
                with self.log:
                    self.write_event({"event": "end", "time": datetime.now().astimezone().isoformat()})
                self.log_message(f"本局已保存 · {self.log_path.name}")
            except OSError as error:
                self.log_message(f"保存本局记录时出错 · {error}")
            finally:
                self.log = None
        self.pact_prompt.pack_forget()
        self.strategy_preview = None
        self.strategy_decision = None
        self.pact = None
        self.last_record = None
        self.readings.clear()
        self.owned.set("")

    def write_event(self, event):
        self.log.write(json.dumps(event, ensure_ascii=False) + "\n")
        self.log.flush()

    def poll(self):
        while not self.output.empty():
            kind, payload = self.output.get_nowait()
            if kind == "done":
                self.worker = None
                self.detect_button.configure(state="normal")
                self.session_state = "idle"
                self.end_session()
            elif kind == "error":
                self.failed_window = self.game[0]
                self.status.set(f"识别出错 · {payload}")
                self.log_message(self.status.get())
            elif kind == "paused" and not self.stop.is_set():
                self.arrow_slots = ()
                self.status.set("识别暂停 · 暂未取得游戏画面，请还原窗口或切回游戏。")
                self.log_message(self.status.get())
            elif kind == "frame" and not self.stop.is_set():
                captured, self.preview_image, lines, details = payload
                if self.status.get().startswith("识别暂停"):
                    self.log_message("已重新取得游戏画面，恢复识别。", captured)
                self.draw_preview()
                readings = analyze_frame(lines, self.regions, self.vocabulary(), details)
                was_active = self.session_state in ("entry", "match")
                self.session_state = session_state_after(self.session_state, readings["phase"])
                active = self.session_state in ("entry", "match")
                if was_active and not active:
                    self.end_session()
                if not active:
                    self.readings = {"phase": readings["phase"]} if readings["phase"] != "未判定" else {}
                    self.status.set("等待进入下一局" if self.session_state == "between" else "等待识别卫戍协议")
                    self.render()
                    continue
                if not was_active:
                    self.begin_session()
                self.readings = readings
                self.update_strategy()
                candidates, owned = self.get_names()
                record = {**self.readings, "preset": self.saved_lineup_name.get() or self.preset_name.get() or None,
                          "recommendation_pact": self.pact, "tracked_operators": self.tracked_operators,
                          "tracked_equipment": self.tracked_equipment,
                          "candidates": sorted(candidates), "owned": sorted(owned)}
                if self.log is not None:
                    self.status.set(f"记录中 · {self.log_path.name}")
                if self.log is not None and record != self.last_record:
                    try:
                        self.write_event({"event": "frame", "time": captured, **record})
                        previous = self.last_record or {}
                        if (record.get("phase"), record.get("round")) != (previous.get("phase"), previous.get("round")):
                            stage = record["phase"]
                            if "round" in record:
                                stage += f" · 第 {record['round']} 回合"
                            self.log_message(stage, captured)
                        if record["recommendation_pact"] and record["recommendation_pact"] != previous.get("recommendation_pact"):
                            self.log_message(f"已启用盟约推荐 · {record['recommendation_pact']}", captured)
                        self.last_record = record
                    except OSError as error:
                        self.status.set(f"写入本局记录失败 · {error}")
                        self.log_message(self.status.get())
                        self.log.close()
                        self.log = None
                self.render()
        if self.closing and self.worker is None:
            self.destroy()
            return
        if not self.avatar_queue.empty():
            self.render()
        self.update_overlay()
        self.after(100, self.poll)

    def update_overlay(self):
        box = None
        if self.worker and not self.stop.is_set() and self.arrow_slots and self.preview_image:
            box = foreground_client_box(self.game[0])
            if box and self.preview_image.size != (box[2] - box[0], box[3] - box[1]):
                box = None
        if box and self.overlay is None:
            self.overlay = RecommendationOverlay(self)
        if self.overlay:
            self.overlay.update(box, self.arrow_slots)

    def update_strategy(self):
        if self.strategy_decision is not None:
            return
        self.strategy_preview, strategy = track_strategy(self.strategy_preview, self.readings)
        pact = STRATEGY_PACTS.get(strategy)
        if not pact:
            return
        self.strategy_decision = pact
        if not self.presets and not self.saved_lineups:
            self.pact = pact
            self.tracked_operators = PACT_OPERATORS[pact]
            self.pact_tabs.select(self.core_pact_tab)
            return
        self.pact_question.configure(text=f"已识别策略「{strategy}」（{pact}）。是否按照盟约推荐干员？")
        next_panel = self.avatar_panel if self.avatar_panel.winfo_manager() else self.tracked_panel
        self.pact_prompt.pack(fill="x", before=next_panel, pady=(0, 8))

    def choose_pact(self, enabled):
        self.pact = self.strategy_decision if enabled else None
        if self.pact:
            self.tracked_operators = PACT_OPERATORS[self.pact]
            self.pact_tabs.select(self.core_pact_tab)
        self.pact_prompt.pack_forget()
        self.render()

    def select_pact(self, pact):
        self.operator_view = "pact"
        self.pact = pact
        if pact in CORE_PACTS:
            self.strategy_decision = pact
            self.pact_prompt.pack_forget()
        self.render()

    def avatar_path(self, name):
        relative = Path("assets") / "avatars" / f"{hashlib.sha1(name.encode('utf-8')).hexdigest()}.png"
        bundled = HERE / relative
        return bundled if bundled.is_file() else DATA_DIR / relative

    def load_avatar(self, name):
        cached = self.avatar_photos.get(name)
        if cached:
            return cached
        path = self.avatar_path(name)
        if path.exists():
            try:
                image = Image.open(path).convert("RGBA")
                image.thumbnail((64, 64), Image.Resampling.LANCZOS)
                photo = ImageTk.PhotoImage(image, master=self)
                self.avatar_photos[name] = photo
                return photo
            except OSError:
                pass
        if name not in self.avatar_pending and name not in self.avatar_failed:
            self.avatar_pending.add(name)
            threading.Thread(target=self.fetch_avatar, args=(name, path), daemon=True).start()
        return None

    def fetch_avatar(self, name, path):
        filename = f"头像_{name}.png"
        encoded = quote(filename)
        digest = hashlib.md5(filename.replace(" ", "_").encode("utf-8")).hexdigest()
        url = (f"https://media.prts.wiki/thumb/{digest[0]}/{digest[:2]}/{encoded}/"
               f"64px-{encoded}")
        try:
            with urlopen(url, timeout=12) as response:
                payload = response.read()
            if not payload.startswith(b"\x89PNG\r\n\x1a\n"):
                raise OSError("图片格式无效")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
            self.avatar_queue.put((name, payload, None))
        except Exception as error:
            self.avatar_queue.put((name, None, str(error)))

    def collect_avatars(self):
        while not self.avatar_queue.empty():
            name, payload, error = self.avatar_queue.get_nowait()
            self.avatar_pending.discard(name)
            if payload:
                try:
                    image = Image.open(io.BytesIO(payload)).convert("RGBA")
                    image.thumbnail((64, 64), Image.Resampling.LANCZOS)
                    self.avatar_photos[name] = ImageTk.PhotoImage(image, master=self)
                    if name == self.detail_operator:
                        self.refresh_detail_portrait()
                except OSError:
                    pass
            elif error:
                self.avatar_failed.add(name)
                self.log_message(f"无法加载 {name} 的 PRTS 头像：{error}")

    def load_equipment_icon(self, name, size=64):
        key = (name, size)
        if key not in self.equipment_photos:
            path = HERE / "assets" / "equipment" / f"{hashlib.sha1(name.encode('utf-8')).hexdigest()}.png"
            try:
                with Image.open(path) as source:
                    icon = ImageOps.contain(source.convert("RGBA"), (size, size), Image.Resampling.LANCZOS)
                    self.equipment_photos[key] = ImageTk.PhotoImage(icon, master=self)
            except OSError:
                return None
        return self.equipment_photos[key]

    def update_avatars(self, panel, tiles, targets, picks, on_click, allow_placeholder=False, *, equipment=False):
        custom = set(self.operator_history)
        if panel is self.avatar_grid and self.operator_view == "search":
            custom.update(self.search_results)
        if equipment:
            targets = sorted(targets, key=EQUIPMENT_RANK.__getitem__)
        else:
            targets = sorted(targets, key=lambda name: 8 if name in custom else OPERATOR_TIERS.get(name, 7))
        while len(tiles) > len(targets):
            tiles.pop().destroy()
        for index, name in enumerate(targets):
            if index == len(tiles):
                tile = tk.Label(panel, width=64, height=64, bd=0,
                                highlightthickness=2, bg="#252a32")
                tile.grid(row=index // 5, column=index % 5, padx=3, pady=3)
                tiles.append(tile)
            tile = tiles[index]
            photo = self.load_equipment_icon(name) if equipment else self.load_avatar(name)
            border = "#f4c542" if name in picks else "#424954"
            if not equipment and name in custom:
                border = "#f28c28"
            clickable = photo is not None or allow_placeholder
            tile.configure(image=photo or self.avatar_placeholder, highlightbackground=border,
                           highlightcolor=border, cursor="hand2")
            if getattr(tile, "operator_binding", None) != (name, clickable):
                tile.bind("<Button-1>", lambda _, operator=name: self.show_operator_details(operator))
                tile.unbind("<Double-Button-1>")
                if clickable:
                    tile.bind("<Double-Button-1>", lambda _, operator=name: on_click(operator))
                tile.operator_binding = (name, clickable)

    def show_operator_search(self):
        self.operator_view = "search"
        self.render()
        self.search_entry.focus_set()

    def show_operator_history(self):
        self.operator_view = "history"
        self.history_page = 0
        self.render()

    def change_history_page(self, offset):
        self.history_page += offset
        self.render()

    def search_operator(self):
        query = normalize(self.search_entry.get()).casefold()
        names = OPERATOR_NAMES | set(self.operator_history)
        self.search_results = tuple(sorted(
            (name for name in names if query and query in normalize(name).casefold()),
            key=lambda name: (normalize(name).casefold() != query, len(name), name)))
        self.avatar_failed.difference_update(self.search_results)
        self.history_page = 0
        self.render()

    def track_operator(self, name):
        if self.operator_view == "search" and name not in self.operator_history:
            self.operator_history.insert(0, name)
            temporary = self.history_path.with_suffix(".tmp")
            try:
                self.history_path.parent.mkdir(parents=True, exist_ok=True)
                temporary.write_text(json.dumps(self.operator_history, ensure_ascii=False, indent=2), encoding="utf-8")
                temporary.replace(self.history_path)
            except OSError as error:
                self.log_message(f"无法保存自选干员历史 · {error}")
        if self.lineup_draft is not None:
            self.lineup_draft = tuple(dict.fromkeys((*self.lineup_draft, name)))
            self.render()
        elif name not in self.tracked_operators:
            self.tracked_operators += (name,)
            self.render()

    def track_pact(self):
        if self.pact:
            members = PACT_OPERATORS[self.pact]
            if self.lineup_draft is not None:
                self.lineup_draft = tuple(dict.fromkeys((*self.lineup_draft, *members)))
            else:
                self.tracked_operators = tuple(dict.fromkeys((*self.tracked_operators, *members)))
            self.render()

    def untrack_operator(self, name):
        self.tracked_operators = tuple(operator for operator in self.tracked_operators if operator != name)
        self.render()

    def clear_tracking(self):
        self.tracked_operators = ()
        self.tracked_equipment = ()
        self.render()

    def track_equipment(self, name):
        if name not in self.tracked_equipment:
            self.tracked_equipment += (name,)
            self.render()

    def untrack_equipment(self, name):
        self.tracked_equipment = tuple(item for item in self.tracked_equipment if item != name)
        self.render()

    def vocabulary(self):
        vocabulary = {name for preset in self.presets.values() for name in preset}
        vocabulary.update(self.tracked_operators)
        return vocabulary | PACT_MEMBERS if self.pact else vocabulary

    def get_names(self):
        vocabulary = self.vocabulary()
        shop = self.readings.get("shop", [])
        if self.pact and self.readings.get("candidate_source") == "自动定位 · 底部卡片":
            candidates = set(shop) & vocabulary
        else:
            candidates = recognize_names(shop, vocabulary)
        owned = recognize_names(self.readings.get("team", []), vocabulary)
        owned.update(filter(None, re.split(r"[\s,，、]+", self.owned.get())))
        return candidates, owned

    def render(self):
        preset = self.preset_name.get()
        active_pacts = {
            str(self.core_pact_tab): CORE_PACTS,
            str(self.supplemental_pact_tab): SUPPLEMENTAL_PACTS,
        }.get(self.pact_tabs.select(), {})
        source = self.pact if self.pact in active_pacts else None
        lineup = active_pacts.get(source, ())
        targets = self.tracked_operators
        candidates, owned = self.get_names()
        picks, _remaining = recommend(targets, candidates, owned)
        equipment_picks = set(self.tracked_equipment) & set(self.readings.get("equipment_candidates", ()))
        for pact, button in self.pact_buttons.items():
            selected = self.pact == pact and self.operator_view == "pact"
            button.configure(text=("✓ " if selected else "") + pact,
                             style="Selected.TButton" if selected else "TButton")
        self.arrow_slots = (
            *(slot["x"] for slot in self.readings.get("operator_slots", ()) if slot["name"] in picks),
            *(slot["x"] for slot in self.readings.get("equipment_slots", ()) if slot["name"] in equipment_picks),
        )
        if self.pact_tabs.select() == str(self.equipment_tab):
            for tier, button in self.equipment_tier_buttons.items():
                button.configure(style="Selected.TButton" if tier == self.equipment_tier else "TButton")
            self.update_avatars(self.equipment_grid, self.equipment_tiles, EQUIPMENT_TIERS[self.equipment_tier],
                                equipment_picks, self.track_equipment, allow_placeholder=True, equipment=True)
        self.avatar_panel.configure(text=f"推荐干员 · {source}" if source else "推荐干员")
        if source == "绝技" and self.operator_view == "pact":
            self.avatar_panel.configure(text="推荐干员 · 绝技（无固定干员）")
        self.collect_avatars()
        saved = self.saved_lineup_name.get()
        preview = saved or preset
        editing = self.lineup_draft is not None
        self.lineup_code_entry.configure(state="disabled" if editing else "normal")
        self.import_code_button.configure(state="disabled" if editing else "normal")
        if preview:
            title = f"阵容干员 · {preview}"
            if editing:
                title += "（编辑中，点击头像移除）"
            self.lineup_panel.configure(text=title)
            self.lineup_panel.pack(fill="x", before=self.pact_panel, pady=(0, 8))
            self.lineup_action.configure(text="取消变更" if editing else "一键追踪")
            self.update_avatars(self.lineup_grid, self.lineup_tiles, self.preview_lineup(),
                                picks, self.click_lineup_operator, allow_placeholder=True)
        else:
            self.lineup_panel.pack_forget()
        if saved:
            if not self.rename_button.winfo_manager():
                self.rename_button.pack(side="left", padx=2)
                self.edit_button.pack(side="left", padx=2)
                self.delete_lineup_button.pack(side="left", padx=2)
                self.export_lineup_button.pack(side="left", padx=2)
            self.rename_button.configure(state="disabled" if editing else "normal")
            self.delete_lineup_button.configure(state="disabled" if editing else "normal")
            self.export_lineup_button.configure(state="disabled" if editing else "normal")
            self.edit_button.configure(text="保存变更" if editing else "变更阵容")
        else:
            self.rename_button.pack_forget()
            self.edit_button.pack_forget()
            self.delete_lineup_button.pack_forget()
            self.export_lineup_button.pack_forget()
        for button, view, label in (
            (self.search_button, "search", "自选干员"),
            (self.history_button, "history", "自选过的干员"),
        ):
            selected = self.operator_view == view
            button.configure(text=("✓ " if selected else "") + label,
                             style="Selected.TButton" if selected else "TButton")
        visible = lineup
        if self.operator_view == "search":
            title = "自选干员 · 输入名称中的任意一部分"
            if self.search_results:
                title = f"自选干员 · 找到 {len(self.search_results)} 名"
            elif self.search_entry.get().strip():
                title = "自选干员 · 没有匹配的名称"
            self.avatar_panel.configure(text=title)
            self.search_bar.pack(fill="x", before=self.avatar_grid, pady=(0, 4))
            visible = self.search_results
        else:
            self.search_bar.pack_forget()
        if self.operator_view == "history":
            self.avatar_panel.configure(text="自选过的干员" if self.operator_history else "自选过的干员 · 暂无记录")
            visible = self.operator_history
        if self.operator_view in ("search", "history"):
            start = self.history_page * 10
            count = len(visible)
            visible = visible[start:start + 10]
            if count > 10:
                self.history_nav.pack(anchor="e")
                self.history_prev.configure(state="normal" if start else "disabled")
                self.history_next.configure(state="normal" if start + 10 < count else "disabled")
            else:
                self.history_nav.pack_forget()
        else:
            self.history_nav.pack_forget()
        if self.operator_view == "pact" and lineup:
            self.track_all_button.configure(text="整组加入阵容" if editing else "一键追踪")
            self.track_all_button.pack(anchor="e", pady=(4, 0))
        else:
            self.track_all_button.pack_forget()
        self.update_avatars(self.avatar_grid, self.avatar_tiles, visible, picks, self.track_operator)
        show_avatars = visible or self.operator_view in ("search", "history") or source == "绝技"
        if show_avatars:
            if not self.avatar_panel.winfo_manager():
                self.avatar_panel.pack(fill="x", before=self.tracked_panel)
        else:
            self.avatar_panel.pack_forget()
        self.update_avatars(self.tracked_grid, self.tracked_tiles, self.tracked_operators,
                            picks, self.untrack_operator, allow_placeholder=True)
        self.update_avatars(self.tracked_equipment_grid, self.tracked_equipment_tiles, self.tracked_equipment,
                            equipment_picks, self.untrack_equipment, allow_placeholder=True, equipment=True)
        title = f"本局追踪干员 · {len(self.tracked_operators)} 名"
        if self.tracked_equipment:
            self.tracked_equipment_grid.pack(fill="x", after=self.tracked_grid, pady=(5, 0))
            title += f" · 装备 {len(self.tracked_equipment)} 件"
        else:
            self.tracked_equipment_grid.pack_forget()
        self.tracked_panel.configure(text=title)
        self.clear_tracking_button.configure(
            state="normal" if self.tracked_operators or self.tracked_equipment else "disabled")
        self.save_lineup_button.configure(state="normal" if self.tracked_operators else "disabled")

    def close(self):
        self.closing = True
        if self.thumbnail:
            self.thumbnail.close()
        self.stop.set()
        self.update_overlay()
        self.withdraw()


if __name__ == "__main__":
    if os.name != "nt":
        raise SystemExit("此工具需要 Windows。")
    ctypes.windll.user32.SetProcessDPIAware()
    Assistant().mainloop()
