"""
テロップに使える日本語フォントの一覧

  ・アプリに同梱した無料フォント（assets/fonts）
  ・パソコンに入っている日本語フォント（Windows の Fonts フォルダ）
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

HERE = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
BUNDLED_DIR = HERE / "assets" / "fonts"
DEFAULT_FAMILY = "Noto Sans JP Black"

# 同梱フォントの分かりやすい名前（ファイル名 → 表示名）
BUNDLED_LABELS = {
    "NotoSansJP-Black.otf": "ゴシック 極太（Noto Sans JP）",
    "ZenKakuGothicNew-Black.ttf": "ゴシック 太（Zen角ゴシック）",
    "DelaGothicOne-Regular.ttf": "インパクト 極太（デラゴシック）",
    "RocknRollOne-Regular.ttf": "ポップ ロックンロール",
    "ReggaeOne-Regular.ttf": "インパクト レゲエ",
    "MochiyPopOne-Regular.ttf": "ポップ もっちー",
    "MPLUSRounded1c-ExtraBold.ttf": "丸ゴシック 太（M PLUS Rounded）",
    "ZenMaruGothic-Black.ttf": "丸ゴシック 極太（Zen丸ゴシック）",
    "KiwiMaru-Medium.ttf": "丸 やわらか（キウイ丸）",
    "HachiMaruPop-Regular.ttf": "手書き風 はちまるポップ",
    "YuseiMagic-Regular.ttf": "手書き風 マジック",
    "Stick-Regular.ttf": "細身 ステッキ",
    "ShipporiMinchoB1-ExtraBold.ttf": "明朝 太（しっぽり明朝）",
    "DotGothic16-Regular.ttf": "ドット（レトロゲーム風）",
}


@dataclass
class FontInfo:
    label: str    # 画面に出す名前
    family: str   # テロップ（ASS）・プレビューで使う名前
    path: str
    bundled: bool


def _names(font) -> tuple[str, str | None]:
    """(英語のファミリー名, 日本語のファミリー名)"""
    n = font["name"]
    en = ja = None
    for r in n.names:
        try:
            if r.nameID == 1 and r.langID == 0x409 and not en:
                en = r.toUnicode()
            elif r.nameID == 1 and r.langID == 0x411 and not ja:
                ja = r.toUnicode()
        except Exception:
            pass
    return (en or n.getDebugName(1) or ""), ja


def _is_japanese(font) -> bool:
    try:
        cmap = font.getBestCmap() or {}
        return all(ord(c) in cmap for c in "あア漢字")
    except Exception:
        return False


def bundled() -> list[FontInfo]:
    from fontTools.ttLib import TTFont
    out = []
    for name, label in BUNDLED_LABELS.items():
        p = BUNDLED_DIR / name
        if p.exists():
            try:
                en, _ = _names(TTFont(p, lazy=True))
            except Exception:
                continue
            out.append(FontInfo(label, en, str(p), True))
    return out


def _system_dirs() -> list[Path]:
    if sys.platform.startswith("win"):
        win = Path(os.getenv("WINDIR", r"C:\Windows")) / "Fonts"
        user = Path(os.getenv("LOCALAPPDATA", "")) / "Microsoft" / "Windows" / "Fonts"
        return [d for d in (win, user) if d.is_dir()]
    if sys.platform == "darwin":
        return [d for d in (Path("/System/Library/Fonts"), Path("/Library/Fonts"), Path.home() / "Library/Fonts") if d.is_dir()]
    return [d for d in (Path("/usr/share/fonts"), Path.home() / ".fonts") if d.is_dir()]


def system(cache_dir: Path | None = None) -> list[FontInfo]:
    """パソコンに入っている日本語フォント（初回だけ少し時間がかかるので結果を保存しておく）"""
    from fontTools.ttLib import TTCollection, TTFont
    cache = (cache_dir / "system_fonts.json") if cache_dir else None
    old = {}
    if cache and cache.exists():
        try:
            old = json.loads(cache.read_text(encoding="utf-8"))
        except Exception:
            old = {}
    new, out, seen = {}, [], set()
    files = []
    for d in _system_dirs():
        files += [p for p in d.rglob("*") if p.suffix.lower() in (".ttf", ".otf", ".ttc")]
    for p in files:
        try:
            st = p.stat()
        except OSError:
            continue
        key = f"{p}|{int(st.st_mtime)}|{st.st_size}"
        if key in old:
            entries = old[key]
        else:
            entries = []
            try:
                fonts = TTCollection(p, lazy=True).fonts if p.suffix.lower() == ".ttc" else [TTFont(p, lazy=True)]
                for f in fonts:
                    if _is_japanese(f):
                        en, ja = _names(f)
                        if en:
                            entries.append([en, ja or en])
            except Exception:
                entries = []
        new[key] = entries
        for en, ja in entries:
            if en not in seen:
                seen.add(en)
                out.append(FontInfo(ja if ja == en else f"{ja}（{en}）", en, str(p), False))
    if cache:
        try:
            cache.write_text(json.dumps(new, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass
    out.sort(key=lambda f: f.label)
    return out


def all_fonts(cache_dir: Path | None = None) -> list[FontInfo]:
    b = bundled()
    names = {f.family for f in b}
    return b + [f for f in system(cache_dir) if f.family not in names]


def files_for(families: set, fonts: list[FontInfo]) -> list[str]:
    """書き出しに必要なフォントファイル"""
    want = set(families) | {DEFAULT_FAMILY}
    return sorted({f.path for f in fonts if f.family in want})
