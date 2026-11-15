"""
切り抜き動画ツールのエンジン（画面なしで動く部分）

  ・analyze()        動画の「盛り上がり」を調べる（音の大きさ・映像の動き・場面の切り替わり）
  ・auto_digest()    全体からまんべんなく良い場面を選び、指定の秒数のダイジェストを作る
  ・render()         カット・テロップ・効果音・音量・縦横を反映して書き出す
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np

FROZEN = getattr(sys, "frozen", False)
HERE = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
CREATE_NO_WINDOW = 0x08000000 if sys.platform.startswith("win") else 0


# ── ffmpeg ───────────────────────────────────────────────────────────
def ffmpeg_bin(name: str = "ffmpeg") -> str:
    exe = name + (".exe" if sys.platform.startswith("win") else "")
    for d in (HERE / "ffmpeg", HERE):
        if (d / exe).exists():
            return str(d / exe)
    found = shutil.which(name)
    if not found:
        raise RuntimeError(f"{name} が見つかりません")
    return found


def run(cmd: list, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, creationflags=CREATE_NO_WINDOW, **kw)


def probe(path: str | Path) -> dict:
    r = run([ffmpeg_bin("ffprobe"), "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)])
    if r.returncode != 0:
        raise RuntimeError("動画を読み込めませんでした：" + r.stderr.decode("utf-8", "ignore")[-300:])
    info = json.loads(r.stdout)
    v = next((s for s in info["streams"] if s.get("codec_type") == "video"), None)
    a = next((s for s in info["streams"] if s.get("codec_type") == "audio"), None)
    if v is None:
        raise RuntimeError("映像が入っていないファイルです")
    dur = float(info["format"].get("duration") or v.get("duration") or 0)
    rot = 0
    for sd in v.get("side_data_list", []) or []:
        if "rotation" in sd:
            rot = int(sd["rotation"])
    w, h = int(v["width"]), int(v["height"])
    if abs(rot) % 180 == 90:
        w, h = h, w
    num, den = (v.get("avg_frame_rate") or "30/1").split("/")
    fps = float(num) / float(den) if float(den) else 30.0
    return {"duration": dur, "width": w, "height": h, "fps": fps or 30.0, "has_audio": a is not None}


def fmt_time(t: float) -> str:
    t = max(0.0, t)
    h, rem = divmod(int(t), 3600)
    m, s = divmod(rem, 60)
    frac = int(round((t - int(t)) * 10)) % 10
    return f"{h}:{m:02d}:{s:02d}.{frac}" if h else f"{m}:{s:02d}.{frac}"


def parse_time(s: str) -> float:
    """「1:23」「1:02:03」「83.5」などを秒に"""
    s = s.strip().replace("：", ":")
    if not s:
        raise ValueError("時間が空です")
    parts = s.split(":")
    t = 0.0
    for p in parts:
        t = t * 60 + float(p)
    return t


# ── データ ───────────────────────────────────────────────────────────
@dataclass
class Caption:
    start: float  # カットの頭からの秒
    end: float
    text: str


@dataclass
class Clip:
    start: float  # 元動画の秒
    end: float
    volume: float = 1.0
    section: int = -1  # おまかせで選んだときの区間番号（場面の入れ替え用）
    captions: list = field(default_factory=list)

    @property
    def length(self) -> float:
        return max(0.0, self.end - self.start)


@dataclass
class Sfx:
    time: float  # 出来上がり動画の頭からの秒
    sound: str   # 内蔵の名前 or ファイルのパス
    volume: float = 1.0


@dataclass
class Project:
    source: str = ""
    clips: list = field(default_factory=list)
    sfx: list = field(default_factory=list)
    aspect: str = "16:9"          # "16:9" / "9:16"
    vertical_fill: str = "blur"   # 縦型のときの埋め方："blur"（ぼかし背景）/ "crop"（中央を切り抜く）
    source_volume: float = 1.0
    normalize: bool = True        # 音量を自動でそろえる
    fade: bool = False            # カットのつなぎをフェードにする
    caption_size: int = 0         # 0=自動
    caption_style: str = "white"  # white / yellow / black

    def total(self) -> float:
        return sum(c.length for c in self.clips)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, indent=1)

    @staticmethod
    def from_json(s: str) -> "Project":
        d = json.loads(s)
        p = Project(**{k: v for k, v in d.items() if k not in ("clips", "sfx")})
        for c in d.get("clips", []):
            caps = [Caption(**x) for x in c.pop("captions", [])]
            p.clips.append(Clip(**c, captions=caps))
        p.sfx = [Sfx(**x) for x in d.get("sfx", [])]
        return p


# ── 解析 ────────────────────────────────────────────────────────────
STEP = 0.5  # 解析の細かさ（秒）


def _read_audio(path, sr=16000, start=None, dur=None) -> np.ndarray:
    cmd = [ffmpeg_bin(), "-v", "error"]
    if start is not None:
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-i", str(path)]
    if dur is not None:
        cmd += ["-t", f"{dur:.3f}"]
    cmd += ["-vn", "-ac", "1", "-ar", str(sr), "-f", "s16le", "-"]
    r = run(cmd)
    return np.frombuffer(r.stdout, np.int16).astype(np.float32) / 32768.0


def analyze(path, progress=None) -> dict:
    """STEP秒ごとの 音の大きさ・動き・場面の切り替わり を返す"""
    info = probe(path)
    dur = info["duration"]
    n = max(1, int(math.ceil(dur / STEP)))
    # 音
    loud = np.full(n, -60.0, np.float32)
    if info["has_audio"]:
        if progress:
            progress(0.02, "音を調べています…")
        a = _read_audio(path)
        hop = int(16000 * STEP)
        k = min(n, len(a) // hop)
        if k:
            rms = np.sqrt((a[: k * hop].reshape(k, hop) ** 2).mean(axis=1) + 1e-10)
            loud[:k] = 20 * np.log10(rms + 1e-10)
    # 映像（2コマ/秒・小さく・白黒）
    if progress:
        progress(0.1, "映像を調べています…")
    W, H = 96, 54
    cmd = [ffmpeg_bin(), "-v", "error", "-i", str(path), "-an", "-vf",
           f"fps={1/STEP},scale={W}:{H},format=gray", "-f", "rawvideo", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW)
    frames = []
    fsz = W * H
    while True:
        buf = proc.stdout.read(fsz * 32)
        if not buf:
            break
        k = len(buf) // fsz
        frames.append(np.frombuffer(buf[: k * fsz], np.uint8).reshape(k, H, W))
        if progress and dur:
            got = sum(len(f) for f in frames) * STEP
            progress(0.1 + 0.85 * min(1.0, got / dur), "映像を調べています…")
    proc.wait()
    fr = np.concatenate(frames) if frames else np.zeros((1, H, W), np.uint8)
    diff = np.zeros(n, np.float32)
    m = min(n, len(fr))
    if m > 1:
        d = np.abs(fr[1:m].astype(np.int16) - fr[: m - 1].astype(np.int16)).mean(axis=(1, 2))
        diff[1:m] = d
    # 場面の切り替わり：動きが周りより飛び抜けて大きい所
    cuts = []
    if m > 4:
        med = np.array([np.median(diff[max(0, i - 6): i + 6]) for i in range(m)])
        for i in range(1, m):
            if diff[i] > max(18, med[i] * 3):
                cuts.append(i * STEP)
    # 平均的な明るさ（真っ暗な場面は避ける）
    bright = np.zeros(n, np.float32)
    bright[:m] = fr[:m].mean(axis=(1, 2))
    if progress:
        progress(1.0, "解析が終わりました")
    return {"duration": dur, "step": STEP, "loud": loud.tolist(), "motion": diff.tolist(),
            "bright": bright.tolist(), "cuts": cuts, "info": info}


def _z(x: np.ndarray) -> np.ndarray:
    s = x.std()
    return (x - x.mean()) / s if s > 1e-6 else np.zeros_like(x)


def scores(an: dict) -> np.ndarray:
    """STEP秒ごとの「良い場面」度"""
    loud = np.asarray(an["loud"], np.float32)
    mot = np.asarray(an["motion"], np.float32)
    br = np.asarray(an["bright"], np.float32)
    k = 3  # なめらかに
    ker = np.ones(k) / k
    loud = np.convolve(loud, ker, "same")
    mot = np.convolve(np.minimum(mot, np.percentile(mot, 98) + 1e-6), ker, "same")
    sc = 0.6 * _z(loud) + 0.4 * _z(mot)
    sc[br < 18] -= 3  # 真っ暗
    loud_floor = np.percentile(loud, 10)
    sc[loud < loud_floor - 1] -= 0.5  # 無音に近い
    return sc


def _snap(t: float, cuts: list, lo: float, hi: float, tol: float = 1.0) -> float:
    """近くに場面の切り替わりがあれば、そこに合わせる"""
    best = None
    for c in cuts:
        if lo <= c <= hi and abs(c - t) <= tol and (best is None or abs(c - t) < abs(best - t)):
            best = c
    return best if best is not None else t


def _quiet_point(loud: np.ndarray, step: float, t: float, lo: float, hi: float, tol: float = 1.0) -> float:
    """t の近く（±tol秒）で一番静かな所。話の途中で切れないようにする"""
    i0 = int(max(lo, t - tol) / step)
    i1 = int(min(hi, t + tol) / step)
    if i1 <= i0:
        return t
    seg = loud[i0:i1 + 1]
    j = int(np.argmin(seg))
    # 十分静かなときだけ動かす（周りより 6dB 以上小さい）
    if seg[j] < np.median(seg) - 6:
        return (i0 + j) * step
    return t


def auto_digest(an: dict, target: float, clip_len: float | None = None,
                ranges: list | None = None, skip_edges: bool = True) -> list[Clip]:
    """
    全体（または ranges の中）を区間に分け、各区間から一番良い場面を1つずつ選ぶ。
    → 前半・中盤・後半からまんべんなく拾うので、全体の雰囲気が伝わる。
    """
    dur = an["duration"]
    step = an["step"]
    sc = scores(an)
    cuts = an["cuts"]
    if not ranges:
        lo, hi = 0.0, dur
        if skip_edges and dur > 120:  # オープニング・エンディングを少し避ける
            lo, hi = dur * 0.02, dur * 0.98
        ranges = [(lo, hi)]
    ranges = [(max(0, a), min(dur, b)) for a, b in ranges if b - a > 0.5]
    total_src = sum(b - a for a, b in ranges)
    if total_src <= 0:
        return []
    target = min(target, total_src)
    if not clip_len:
        # 長さに合わせて1カットの長さを自動で（短い動画ほどテンポよく）
        clip_len = float(np.clip(target / 10, 2.5, 6.0))
    clip_len = min(clip_len, target)
    n = max(1, int(round(target / clip_len)))
    clip_len = target / n
    # 区間を「元動画の長さに比例して」各範囲に割り振る
    sections = []
    for a, b in ranges:
        k = max(1, int(round(n * (b - a) / total_src)))
        edges = np.linspace(a, b, k + 1)
        sections += [(edges[i], edges[i + 1]) for i in range(k)]
    sections = sections[:n] if len(sections) > n else sections
    while len(sections) < n:  # 足りなければ一番長い区間を割る
        i = max(range(len(sections)), key=lambda j: sections[j][1] - sections[j][0])
        a, b = sections.pop(i)
        sections[i:i] = [(a, (a + b) / 2), ((a + b) / 2, b)]
    clips = []
    win = max(1, int(round(clip_len / step)))
    csum = np.concatenate([[0], np.cumsum(sc)])
    for si, (a, b) in enumerate(sections):
        if b - a <= clip_len:
            s = a
        else:
            i0, i1 = int(a / step), int((b - clip_len) / step)
            best_i, best_v = i0, -1e9
            for i in range(i0, max(i0 + 1, i1 + 1)):
                v = csum[min(len(sc), i + win)] - csum[min(len(sc), i)]
                if v > best_v:
                    best_i, best_v = i, v
            s = best_i * step
        s = _snap(s, cuts, a, b - clip_len * 0.6)
        loud = np.asarray(an["loud"], np.float32)
        s2 = _quiet_point(loud, step, s, a, s + clip_len * 0.3)
        e = min(b, s2 + clip_len)
        e = _quiet_point(loud, step, e, s2 + clip_len * 0.7, min(b, s2 + clip_len * 1.4))
        clips.append(Clip(start=round(s2, 2), end=round(min(e, dur), 2), section=si))
    return clips


def alternatives(an: dict, clip: Clip, length: float | None = None, count: int = 3,
                 lo: float | None = None, hi: float | None = None) -> list[float]:
    """同じあたりから、別の良い場面の候補（開始秒）を返す（「別の場面に替える」用）"""
    step = an["step"]
    sc = scores(an)
    L = length or clip.length
    lo = max(0.0, clip.start - 60 if lo is None else lo)
    hi = min(an["duration"], clip.end + 60 if hi is None else hi)
    win = max(1, int(round(L / step)))
    csum = np.concatenate([[0], np.cumsum(sc)])
    cands = []
    for i in range(int(lo / step), int((hi - L) / step) + 1):
        v = csum[min(len(sc), i + win)] - csum[min(len(sc), i)]
        cands.append((v, i * step))
    cands.sort(reverse=True)
    out = []
    for v, s in cands:
        if abs(s - clip.start) < L * 0.8:
            continue
        if all(abs(s - o) >= L * 0.8 for o in out):
            out.append(s)
        if len(out) >= count:
            break
    return out


# ── 書き出し ─────────────────────────────────────────────────────────
def _ass_time(t: float) -> str:
    t = max(0.0, t)
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


NO_HEAD = set("、。，．！？!?）」』】〉》ー～…・ぁぃぅぇぉっゃゅょゎァィゥェォッャュョヮ")


def wrap_text(text: str, per_line: int, max_lines: int = 2) -> str:
    """テロップの改行（句読点や小さい「っ」などが行頭に来ないようにする）"""
    text = " ".join(text.strip().split())
    lines, cur = [], ""
    i = 0
    while i < len(text):
        cur += text[i]
        i += 1
        if len(cur) >= per_line and i < len(text):
            # 次の文字が行頭禁止なら、この行に含める
            while i < len(text) and text[i] in NO_HEAD:
                cur += text[i]
                i += 1
            # 句読点・空白の直後で折り返せるなら、そこで
            cut = max(cur.rfind(c) for c in "、。！？!? ")
            if per_line * 0.5 <= cut < len(cur) - 1:
                i -= len(cur) - cut - 1
                cur = cur[: cut + 1]
            lines.append(cur.strip())
            cur = ""
    if cur.strip():
        lines.append(cur.strip())
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1][:-1] + "…"
    return "\\N".join(lines)


STYLES = {  # (文字色, 縁取り色) ASS は &HAABBGGRR
    "white": ("&H00FFFFFF", "&H00000000"),
    "yellow": ("&H0000F0FF", "&H00000000"),
    "black": ("&H00000000", "&H00FFFFFF"),
    "pink": ("&H00B469FF", "&H00FFFFFF"),
}


def build_ass(project: Project, W: int, H: int, font: str) -> str:
    vertical = H > W
    size = project.caption_size or (int(W * 0.075) if vertical else int(H * 0.068))
    per_line = 13 if vertical else 22
    max_lines = 3 if vertical else 2
    prim, outl = STYLES.get(project.caption_style, STYLES["white"])
    margin_v = int(H * (0.22 if vertical else 0.07))
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{font},{size},{prim},&H000000FF,{outl},&H80000000,0,0,0,0,100,100,0,0,1,{max(2, size // 11)},{max(1, size // 30)},2,{int(W*0.05)},{int(W*0.05)},{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = []
    t0 = 0.0
    for c in project.clips:
        for cap in c.captions:
            s = t0 + max(0.0, cap.start)
            e = t0 + min(c.length, cap.end)
            if e - s > 0.05 and cap.text.strip():
                txt = wrap_text(cap.text, per_line, max_lines).replace("{", "（").replace("}", "）")
                lines.append(f"Dialogue: 0,{_ass_time(s)},{_ass_time(e)},Default,,0,0,0,,{txt}")
        t0 += c.length
    return head + "\n".join(lines) + "\n"


def output_size(project: Project, preview: bool = False) -> tuple[int, int]:
    if project.aspect == "9:16":
        W, H = 1080, 1920
    else:
        W, H = 1920, 1080
    if preview:
        W, H = W // 3 // 2 * 2, H // 3 // 2 * 2
    return W, H


def render(project: Project, out_path: str | Path, progress=None, preview: bool = False,
           font_dir: str | None = None, font_name: str = "Noto Sans JP Black", sfx_dir: str | None = None) -> Path:
    """プロジェクトを書き出す（preview=True なら小さく速く）"""
    if not project.clips:
        raise RuntimeError("カットがありません")
    src = project.source
    info = probe(src)
    W, H = output_size(project, preview)
    fps = 30
    tmp = Path(tempfile.mkdtemp(prefix="kirinuki_"))
    cmd = [ffmpeg_bin(), "-y", "-hide_banner", "-loglevel", "error", "-progress", "pipe:1", "-nostats"]
    for c in project.clips:
        cmd += ["-ss", f"{c.start:.3f}", "-t", f"{c.length:.3f}", "-i", src]
    n = len(project.clips)
    sfx_inputs = []
    for s in project.sfx:
        p = resolve_sfx(s.sound, sfx_dir)
        if p and Path(p).exists():
            sfx_inputs.append((s, len(project.clips) + len(sfx_inputs)))
            cmd += ["-i", str(p)]
    fl = []
    for i, c in enumerate(project.clips):
        L = c.length
        same_shape = abs(info["width"] / info["height"] - W / H) < 0.05
        if not same_shape and project.vertical_fill == "blur":
            # 縦横の形が違う：ぼかした映像で余白を埋める
            fl.append(f"[{i}:v]fps={fps},split=2[b{i}][f{i}];"
                      f"[b{i}]scale={W//4}:{H//4}:force_original_aspect_ratio=increase,crop={W//4}:{H//4},"
                      f"boxblur=8:2,eq=brightness=-0.08,scale={W}:{H}[bb{i}];"
                      f"[f{i}]scale={W}:{H}:force_original_aspect_ratio=decrease[ff{i}];"
                      f"[bb{i}][ff{i}]overlay=(W-w)/2:(H-h)/2,setsar=1,format=yuv420p,setpts=PTS-STARTPTS[v{i}]")
        else:
            # 同じ形 or 「中央を切り抜く」
            fl.append(f"[{i}:v]fps={fps},scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
                      f"setsar=1,format=yuv420p,setpts=PTS-STARTPTS[v{i}]")
        if project.fade and L > 0.6:
            fl[-1] = fl[-1].replace(f"setpts=PTS-STARTPTS[v{i}]",
                                    f"setpts=PTS-STARTPTS,fade=t=in:st=0:d=0.2,fade=t=out:st={L-0.2:.3f}:d=0.2[v{i}]")
        vol = c.volume * project.source_volume
        if info["has_audio"]:
            af = f"[{i}:a]aresample=48000,aformat=channel_layouts=stereo,asetpts=PTS-STARTPTS,"
        else:
            af = f"anullsrc=r=48000:cl=stereo,atrim=0:{L:.3f},"
        fd = 0.2 if project.fade else 0.02
        fl.append(af + f"volume={vol:.3f},afade=t=in:st=0:d={fd},afade=t=out:st={max(0, L-fd):.3f}:d={fd},"
                  f"apad,atrim=0:{L:.3f}[a{i}]")
    fl.append("".join(f"[v{i}][a{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=1[vc][ac]")
    # テロップ
    vout = "vc"
    has_caps = any(cap.text.strip() for c in project.clips for cap in c.captions)
    if has_caps:
        (tmp / "subs.ass").write_text(build_ass(project, W, H, font_name), encoding="utf-8")
        fd_opt = f":fontsdir=fonts" if font_dir else ""
        if font_dir:
            shutil.copytree(Path(font_dir).resolve(), tmp / "fonts", dirs_exist_ok=True)
        fl.append(f"[vc]subtitles=subs.ass{fd_opt}[vs]")
        vout = "vs"
    # 効果音
    aout = "ac"
    if sfx_inputs:
        parts = ["[ac]"]
        for k, (s, idx) in enumerate(sfx_inputs):
            ms = int(max(0, s.time) * 1000)
            fl.append(f"[{idx}:a]aresample=48000,aformat=channel_layouts=stereo,volume={s.volume:.3f},"
                      f"adelay={ms}|{ms}[s{k}]")
            parts.append(f"[s{k}]")
        fl.append("".join(parts) + f"amix=inputs={len(parts)}:normalize=0:duration=first[am]")
        aout = "am"
    if project.normalize and not preview:
        fl.append(f"[{aout}]loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000[an]")
        aout = "an"
    (tmp / "graph.txt").write_text(";\n".join(fl), encoding="utf-8")
    cmd += ["-filter_complex_script", "graph.txt", "-map", f"[{vout}]", "-map", f"[{aout}]",
            "-c:v", "libx264", "-preset", "ultrafast" if preview else "veryfast", "-crf", "28" if preview else "20",
            "-r", str(fps), "-c:a", "aac", "-b:a", "128k" if preview else "192k", "-movflags", "+faststart",
            str(Path(out_path).resolve())]
    total = project.total()
    proc = subprocess.Popen(cmd, cwd=tmp, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            creationflags=CREATE_NO_WINDOW, text=True, encoding="utf-8", errors="ignore")
    for line in proc.stdout:
        if line.startswith("out_time_ms=") and progress and total:
            try:
                progress(min(1.0, int(line.split("=")[1]) / 1e6 / total))
            except ValueError:
                pass
    err = proc.stderr.read()
    proc.wait()
    shutil.rmtree(tmp, ignore_errors=True)
    if proc.returncode != 0:
        raise RuntimeError("書き出しに失敗しました：" + err[-600:])
    if progress:
        progress(1.0)
    return Path(out_path)


# ── 効果音 ───────────────────────────────────────────────────────────
BUILTIN_SFX = {
    "ピコン": "pikon.wav", "ドン": "don.wav", "シュッ": "shu.wav", "ポン": "pon.wav",
    "キラーン": "kiran.wav", "ブブー": "bubu.wav", "ジャン！": "jan.wav", "ドキッ": "doki.wav",
}


def resolve_sfx(sound: str, sfx_dir: str | None = None) -> str | None:
    if sound in BUILTIN_SFX:
        d = Path(sfx_dir) if sfx_dir else HERE / "assets" / "sfx"
        return str((d / BUILTIN_SFX[sound]).resolve())
    return str(Path(sound).resolve()) if sound else None


def thumbnail(path, t: float, w: int = 240) -> bytes:
    """指定秒の小さな画像（JPEG）"""
    r = run([ffmpeg_bin(), "-v", "error", "-ss", f"{max(0, t):.3f}", "-i", str(path), "-frames:v", "1",
             "-vf", f"scale={w}:-2", "-f", "image2", "-c:v", "mjpeg", "-q:v", "4", "-"])
    return r.stdout
