"""
自動文字起こし（faster-whisper）→ テロップ

モデルは初回だけダウンロードして、ユーザーのフォルダに保存する。
"""

from __future__ import annotations

import os
import sys
import urllib.request
import zipfile
from pathlib import Path

import numpy as np

import engine as E

REPO = "04tetsu03ya/kirinuki-tool"
MODELS = {
    # 名前: (表示名, ダウンロードの目安MB)
    "small": ("標準（速い）", 460),
    "turbo": ("高精度（遅い）", 1600),
}


def data_dir() -> Path:
    if sys.platform.startswith("win"):
        base = Path(os.getenv("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.getenv("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    d = base / "KirinukiTool"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _ssl_context():
    import ssl
    try:
        import truststore
        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except Exception:
        pass
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def model_dir(name: str, progress=None) -> Path:
    """モデルの場所。無ければダウンロードして展開する"""
    d = data_dir() / "models" / f"whisper-{name}"
    if (d / "model.bin").exists():
        return d
    d.parent.mkdir(parents=True, exist_ok=True)
    url = f"https://github.com/{REPO}/releases/download/models/whisper-{name}.zip"
    tmp = d.parent / f"whisper-{name}.zip.part"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "KirinukiTool"})
        with urllib.request.urlopen(req, context=_ssl_context(), timeout=60) as r, open(tmp, "wb") as fh:
            total = int(r.headers.get("Content-Length") or 0)
            done = 0
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                fh.write(chunk)
                done += len(chunk)
                if progress and total:
                    progress(done / total * 0.95, f"文字起こしAIをダウンロード中… {done // 1_000_000}/{total // 1_000_000}MB（初回のみ）")
        with zipfile.ZipFile(tmp) as z:
            z.extractall(d)
    except Exception as e:
        raise RuntimeError("文字起こしAIのダウンロードに失敗しました。インターネット接続を確認してください。"
                           f"\n（詳細: {e}）") from None
    finally:
        tmp.unlink(missing_ok=True)
    return d


_cache: dict = {}


def load_model(name: str = "small", progress=None):
    if name not in _cache:
        from faster_whisper import WhisperModel
        path = model_dir(name, progress)
        if progress:
            progress(0.97, "文字起こしAIを準備中…")
        threads = max(1, (os.cpu_count() or 4) - 1)
        _cache[name] = WhisperModel(str(path), device="cpu", compute_type="int8", cpu_threads=threads)
    return _cache[name]


# 無音や雑音のときに文字起こしAIが勝手に作ってしまう決まり文句
PHANTOM = ("ご視聴ありがとうございました", "ご覧いただき", "字幕", "チャンネル登録", "お疲れ様でした",
           "最後までご視聴", "次回もお楽しみに", "Thank you for watching", "Subtitles by")


def is_phantom(seg) -> bool:
    t = seg.text.strip()
    if getattr(seg, "no_speech_prob", 0) > 0.6 or getattr(seg, "avg_logprob", 0) < -1.0:
        return True
    if getattr(seg, "compression_ratio", 1) > 2.6:  # 同じ言葉の繰り返し
        return True
    return any(p in t for p in PHANTOM)


def split_segment(text: str, start: float, end: float, max_chars: int) -> list[E.Caption]:
    """長い発言を、読みやすい長さのテロップに分ける（時間は文字数で按分）"""
    text = text.strip()
    if len(text) <= max_chars:
        return [E.Caption(round(start, 2), round(end, 2), text)]
    pieces, cur = [], ""
    for ch in text:
        cur += ch
        if len(cur) >= max_chars * 0.6 and ch in "、。！？!? ":
            pieces.append(cur.strip()); cur = ""
        elif len(cur) >= max_chars:
            pieces.append(cur.strip()); cur = ""
    if cur.strip():
        pieces.append(cur.strip())
    total = sum(len(p) for p in pieces) or 1
    out, t = [], start
    for p in pieces:
        d = (end - start) * len(p) / total
        out.append(E.Caption(round(t, 2), round(t + d, 2), p))
        t += d
    return out


def transcribe_clips(source: str, clips: list, model: str = "small", language: str = "ja",
                     max_chars: int = 26, progress=None, cancel=None) -> int:
    """各カットの音声を文字起こしして、カットのテロップを置き換える。追加したテロップの数を返す"""
    m = load_model(model, progress)
    count = 0
    for i, c in enumerate(clips):
        if cancel and cancel():
            break
        if progress:
            progress(i / max(1, len(clips)), f"文字起こし中… {i + 1}/{len(clips)}")
        pad = 0.3
        s0 = max(0.0, c.start - pad)
        audio = E._read_audio(source, 16000, s0, c.length + 2 * pad)
        if len(audio) < 1600:
            continue
        segs, _ = m.transcribe(audio, language=language, vad_filter=True, beam_size=5,
                               condition_on_previous_text=False)
        caps = []
        for sg in segs:
            st = sg.start + s0 - c.start
            en = sg.end + s0 - c.start
            st, en = max(0.0, st), min(c.length, en)
            if en - st < 0.2 or not sg.text.strip() or is_phantom(sg):
                continue
            caps += split_segment(sg.text, st, en, max_chars)
        c.captions = caps
        count += len(caps)
    if progress:
        progress(1.0, f"文字起こしが終わりました（{count}件）")
    return count
