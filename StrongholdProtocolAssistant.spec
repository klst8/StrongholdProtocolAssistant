from pathlib import Path
from PyInstaller.utils.win32.versioninfo import (
    FixedFileInfo, StringFileInfo, StringStruct, StringTable, VarFileInfo, VarStruct, VSVersionInfo,
)

root = Path(SPECPATH)
version = (1, 1, 0, 0)
version_text = ".".join(str(part) for part in version[:3])
windows_version = VSVersionInfo(
    ffi=FixedFileInfo(filevers=version, prodvers=version, OS=0x40004, fileType=0x1),
    kids=[
        StringFileInfo([StringTable("080404B0", [
            StringStruct("FileDescription", "卫戍选调助手"),
            StringStruct("FileVersion", version_text),
            StringStruct("ProductName", "卫戍选调助手"),
            StringStruct("ProductVersion", version_text),
        ])]),
        VarFileInfo([VarStruct("Translation", [0x0804, 1200])]),
    ],
)
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
    version=windows_version,
    icon=str(root / "assets" / "app_icon.ico"),
    console=False,
    upx=False,
)
