from pathlib import Path

root = Path(SPECPATH)
files = (
    "ocr.ps1",
    "pact_recommendations.json",
    "supplemental_pacts.json",
    "operator_names.json",
    "operator_tiers.json",
    "operator_traits.json",
    "equipment_tiers.json",
    "equipment_details.json",
)
datas = [(str(root / name), ".") for name in files]
datas.append((str(root / "assets"), "assets"))

a = Analysis([str(root / "assistant.py")], pathex=[str(root)], datas=datas)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    name="StrongholdProtocolAssistant",
    icon=str(root / "assets" / "app_icon.ico"),
    console=False,
    upx=False,
)
