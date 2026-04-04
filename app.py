#!/usr/bin/env python3
"""
ショート動画 自動生成 Webアプリ
起動: python app.py
ブラウザ: http://localhost:8000
"""

import os, sys, json, re, uuid, subprocess, urllib.request, shutil, threading, time
from pathlib import Path
from fastapi import FastAPI, UploadFile, File, Form, BackgroundTasks, HTTPException
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

# ── 設定 ────────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
WORK_DIR   = Path("./web_output")

# アップロード検証
ALLOWED_THUMB_EXTS    = {".jpg", ".jpeg", ".png", ".webp"}
ALLOWED_THUMB_MAGIC   = {b"\xff\xd8\xff", b"\x89PNG", b"RIFF", b"WEBP"}  # JPEG/PNG/WEBP
MAX_THUMB_BYTES       = 10 * 1024 * 1024  # 10MB

# ジョブTTL: 完了から1時間でディレクトリ削除
JOB_TTL_SECONDS = 3600

# ffmpegタイムアウト
FFMPEG_TIMEOUT = 300  # 5分
SHORT_W    = 720
SHORT_H    = 1280
HEADER_H   = 260
VIDEO_H    = 530
THUMB_H    = 490
BORDER     = 8

FONTS = [
    "/System/Library/Fonts/ヒラギノ角ゴシック W6.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
]

def find_font():
    for f in FONTS:
        if os.path.exists(f): return f
    r = subprocess.run(["fc-list",":lang=ja","--format=%{file}\n"], capture_output=True, text=True)
    for line in r.stdout.splitlines():
        if line.strip() and os.path.exists(line.strip()): return line.strip()
    return None

# ── ジョブ状態管理 ───────────────────────────────────────────
jobs: dict[str, dict] = {}

def log(job_id: str, msg: str):
    jobs[job_id]["logs"].append(msg)
    print(f"[{job_id[:6]}] {msg}")

def set_progress(job_id: str, pct: int, status: str = "running"):
    jobs[job_id]["progress"] = pct
    jobs[job_id]["status"]   = status

# ── メイン処理（バックグラウンド） ──────────────────────────
def run_job(job_id: str, youtube_url: str, thumb_path: str,
            channel: str, title: str, num_clips: int,
            clip_duration: int, instruction: str):
    job_dir = WORK_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    try:
        log(job_id, "📥 動画をダウンロード中...")
        set_progress(job_id, 5)
        tmpl = str(job_dir / "source.%(ext)s")
        r = subprocess.run(
            ["yt-dlp", "-f",
             "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
             "--merge-output-format", "mp4", "-o", tmpl, youtube_url],
            capture_output=True, text=True
        )
        if r.returncode != 0:
            raise RuntimeError(f"ダウンロード失敗: {r.stderr[:200]}")

        video_path = next(job_dir.glob("source.*"), None)
        if not video_path:
            raise RuntimeError("動画ファイルが見つかりません")
        log(job_id, f"✅ ダウンロード完了: {video_path.name}")
        set_progress(job_id, 25)

        probe = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_format", "-show_streams", str(video_path)],
            capture_output=True, text=True
        )
        info = json.loads(probe.stdout)
        duration = float(info["format"]["duration"])
        src_w, src_h = next(
            (int(s["width"]), int(s["height"]))
            for s in info["streams"] if s["codec_type"] == "video"
        )
        log(job_id, f"📐 動画情報: {src_w}x{src_h}, {duration:.0f}秒")

        log(job_id, "🤖 Claudeがシーンを分析中...")
        set_progress(job_id, 35)
        clips = analyze_with_claude(duration, title, num_clips, clip_duration, instruction, job_id)
        log(job_id, f"✅ {len(clips)}件のシーンを検出")
        set_progress(job_id, 50)

        font_path = find_font()
        log(job_id, f"🔤 フォント: {Path(font_path).name if font_path else 'なし'}")

        results = []
        for i, clip in enumerate(clips):
            pct = 50 + int((i / len(clips)) * 45)
            set_progress(job_id, pct)
            out_path = job_dir / f"short_{clip['rank']:02d}.mp4"
            log(job_id, f"✂️  [{i+1}/{len(clips)}] {clip['title']} ({clip['start_seconds']:.0f}s〜{clip['end_seconds']:.0f}s)")

            ok = build_short(
                video_path=str(video_path),
                thumb_path=thumb_path,
                start=clip["start_seconds"],
                end=clip["end_seconds"],
                channel_name=channel,
                title_text=title,
                out_path=str(out_path),
                font_path=font_path,
                src_w=src_w, src_h=src_h,
            )
            if ok:
                size_mb = round(out_path.stat().st_size / 1024 / 1024, 1)
                results.append({
                    **clip,
                    "filename": out_path.name,
                    "size_mb": size_mb,
                    "download_url": f"/download/{job_id}/{out_path.name}"
                })
                log(job_id, f"   ✅ 完成 ({size_mb}MB)")
            else:
                log(job_id, f"   ❌ 合成失敗")

        jobs[job_id]["results"] = results
        set_progress(job_id, 100, "done")
        log(job_id, f"🎉 完了！{len(results)}件のショート動画を生成しました")

    except Exception as e:
        jobs[job_id]["error"] = str(e)
        set_progress(job_id, -1, "error")
        log(job_id, f"❌ エラー: {e}")


def analyze_with_claude(duration: float, title: str, num_clips: int,
                        clip_duration: int, instruction: str, job_id: str) -> list[dict]:
    import anthropic
    if not ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY が設定されていません。")

    margin = max(5, clip_duration // 6)
    dur_min = max(10, clip_duration - margin)
    dur_max = clip_duration + margin

    instruction_line = f"\n切り抜き指示: {instruction}" if instruction.strip() else ""

    prompt = f"""以下の動画から、ショート動画として魅力的なシーンを{num_clips}箇所選んでください。

動画タイトル: {title}
動画の長さ: {duration:.0f}秒
切り抜き目標: 各シーン約{clip_duration}秒（{dur_min}〜{dur_max}秒の範囲）{instruction_line}

条件:
- 視聴者が最後まで見たくなる、内容的に完結したシーンを選ぶ
- 開始・終了が話の途中にならないよう考慮する
- シーン同士が重複しないようにする

以下のJSON形式のみで回答してください（説明文や```は不要）:
{{"clips":[{{"rank":1,"title":"タイトル（20文字以内）","start_seconds":30.0,"end_seconds":65.0,"reason":"理由（50文字以内）"}}]}}"""

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    res = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=800,
        messages=[{"role": "user", "content": prompt}]
    )
    text = re.sub(r"```json|```", "", res.content[0].text).strip()
    clips = json.loads(text).get("clips", [])
    for c in clips:
        length = c["end_seconds"] - c["start_seconds"]
        if length < dur_min: c["end_seconds"] = c["start_seconds"] + dur_min
        if length > dur_max: c["end_seconds"] = c["start_seconds"] + dur_max
        c["end_seconds"] = min(c["end_seconds"], duration)
    return clips


def build_short(video_path, thumb_path, start, end, channel_name, title_text,
                out_path, font_path, src_w, src_h):
    W, H = SHORT_W, SHORT_H
    duration = end - start
    gold, dark_red = "0xD4AF37", "0x7B1F2E"
    title_box_y, title_box_h = 80, 150
    video_y  = HEADER_H
    thumb_y  = HEADER_H + VIDEO_H

    va_w = W - BORDER * 2
    va_h = VIDEO_H - BORDER * 2
    sc_h = int(va_w * src_h / src_w)
    if sc_h < va_h:
        sc_w, sc_h = int(va_h * src_w / src_h), va_h
    else:
        sc_w = va_w
    cx, cy = (sc_w - va_w) // 2, (sc_h - va_h) // 2

    fc = (
        f"color=c=black:s={W}x{H}:d={duration}[bg];"
        f"color=c={dark_red}:s={W-40}x{title_box_h}:d={duration}[tb];"
        f"[bg][tb]overlay=x=20:y={title_box_y}[bg1];"
        f"color=c={gold}:s={W}x{VIDEO_H}:d={duration}[gf];"
        f"[bg1][gf]overlay=x=0:y={video_y}[bg2];"
        f"[0:v]scale={sc_w}:{sc_h},crop={va_w}:{va_h}:{cx}:{cy}[vid];"
        f"[bg2][vid]overlay=x={BORDER}:y={video_y+BORDER}[bg3];"
        f"[1:v]scale={W}:{THUMB_H}[th];"
        f"[bg3][th]overlay=x=0:y={thumb_y}[out]"
    )

    if font_path:
        # ffmpegフィルタ注入防止: テキストをファイル経由で渡す（textfile=オプション）
        import tempfile, atexit
        ef = font_path.replace("\\", "\\\\").replace(":", "\\:")
        txt_dir = Path(out_path).parent

        ch_file  = txt_dir / "ch.txt"
        tit1_file = txt_dir / "tit1.txt"
        tit2_file = txt_dir / "tit2.txt"

        ch_file.write_text(channel_name, encoding="utf-8")

        raw_title = title_text
        fc = fc.replace("[out]", "[pt]")
        if len(raw_title) > 12:
            mid = len(raw_title) // 2
            sp  = raw_title.find(" ", mid) if " " in raw_title[mid:] else mid
            t1, t2 = raw_title[:sp].strip(), raw_title[sp:].strip()
            tit1_file.write_text(t1, encoding="utf-8")
            tit2_file.write_text(t2, encoding="utf-8")
            ef1 = str(tit1_file).replace("\\", "\\\\").replace(":", "\\:")
            ef2 = str(tit2_file).replace("\\", "\\\\").replace(":", "\\:")
            td = (f"drawtext=fontfile='{ef}':textfile='{ef1}':fontsize=46:fontcolor=white"
                  f":x=(w-text_w)/2:y={title_box_y+20},"
                  f"drawtext=fontfile='{ef}':textfile='{ef2}':fontsize=46:fontcolor=white"
                  f":x=(w-text_w)/2:y={title_box_y+78}")
        else:
            tit1_file.write_text(raw_title, encoding="utf-8")
            ef1 = str(tit1_file).replace("\\", "\\\\").replace(":", "\\:")
            td = (f"drawtext=fontfile='{ef}':textfile='{ef1}':fontsize=46:fontcolor=white"
                  f":x=(w-text_w)/2:y={title_box_y+40}")

        ech_path = str(ch_file).replace("\\", "\\\\").replace(":", "\\:")
        fc += (f";[pt]drawtext=fontfile='{ef}':textfile='{ech_path}':fontsize=30:fontcolor=white"
               f":x=(w-text_w)/2:y=28,{td}[out]")

    cmd = [
        "ffmpeg", "-y",
        "-ss", str(start), "-t", str(duration), "-i", video_path,
        "-loop", "1", "-i", thumb_path,
        "-filter_complex", fc,
        "-map", "[out]", "-map", "0:a?",
        "-c:v", "libx264", "-crf", "18", "-preset", "fast",
        "-c:a", "aac", "-shortest", "-movflags", "+faststart",
        out_path
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=FFMPEG_TIMEOUT)
    return r.returncode == 0


app = FastAPI()
WORK_DIR.mkdir(exist_ok=True)

@app.post("/api/generate")
async def generate(
    background_tasks: BackgroundTasks,
    youtube_url: str = Form(...),
    channel: str = Form("チャンネル名"),
    title: str = Form("タイトル"),
    num_clips: int = Form(3),
    clip_duration: int = Form(35),
    instruction: str = Form(""),
    thumbnail: UploadFile = File(...)
):
    job_id = str(uuid.uuid4())
    job_dir = WORK_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    ext = Path(thumbnail.filename).suffix or ".jpg"
    thumb_path = str(job_dir / f"thumb{ext}")
    with open(thumb_path, "wb") as f:
        f.write(await thumbnail.read())

    jobs[job_id] = {"status": "running", "progress": 0, "logs": [], "results": []}
    background_tasks.add_task(
        run_job, job_id, youtube_url, thumb_path, channel, title, num_clips,
        clip_duration, instruction
    )
    return {"job_id": job_id}

@app.get("/api/status/{job_id}")
def status(job_id: str):
    if job_id not in jobs:
        return JSONResponse({"error": "not found"}, status_code=404)
    return jobs[job_id]

@app.get("/download/{job_id}/{filename}")
def download(job_id: str, filename: str):
    # パストラバーサル防止: job_id・filename にスラッシュ・ドットドットを含む場合は拒否
    if "/" in job_id or ".." in job_id or "/" in filename or ".." in filename:
        return JSONResponse({"error": "invalid path"}, status_code=400)
    path = (WORK_DIR / job_id / filename).resolve()
    base = WORK_DIR.resolve()
    if not str(path).startswith(str(base) + "/"):
        return JSONResponse({"error": "invalid path"}, status_code=400)
    if not path.exists():
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(str(path), media_type="video/mp4",
                        headers={"Content-Disposition": f'attachment; filename="{path.name}"'})

@app.get("/", response_class=HTMLResponse)
def index():
    return HTML

HTML = """<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ショート動画 自動生成</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Hiragino Sans',sans-serif;background:#0f0f0f;color:#eee;min-height:100vh;display:flex;align-items:flex-start;justify-content:center;padding:40px 16px}
.wrap{width:100%;max-width:640px}
h1{font-size:22px;font-weight:600;margin-bottom:4px;color:#fff}
.sub{font-size:13px;color:#888;margin-bottom:32px}
.card{background:#1a1a1a;border:1px solid #2a2a2a;border-radius:14px;padding:24px;margin-bottom:16px}
.card h2{font-size:13px;font-weight:600;color:#888;text-transform:uppercase;letter-spacing:.06em;margin-bottom:16px}
label{display:block;font-size:13px;color:#aaa;margin-bottom:6px;margin-top:14px}
label:first-of-type{margin-top:0}
input[type=text],input[type=url],input[type=number]{width:100%;background:#111;border:1px solid #333;border-radius:8px;padding:10px 12px;font-size:14px;color:#fff;outline:none;transition:border .15s}
input:focus{border-color:#555}
textarea{width:100%;background:#111;border:1px solid #333;border-radius:8px;padding:10px 12px;font-size:14px;color:#fff;outline:none;transition:border .15s;resize:vertical;min-height:72px;font-family:inherit}
textarea:focus{border-color:#555}
.slider-wrap{display:flex;align-items:center;gap:10px}
input[type=range]{flex:1;accent-color:#fff;height:4px;cursor:pointer}
.slider-val{font-size:14px;color:#fff;font-weight:600;min-width:46px;text-align:right}
.dur-row{display:flex;align-items:center;gap:10px;margin-top:10px}
.dur-row label{margin:0;white-space:nowrap}
.dur-row input[type=number]{width:90px;flex-shrink:0}
.thumb-zone{border:1.5px dashed #333;border-radius:10px;padding:32px;text-align:center;cursor:pointer;transition:all .15s;position:relative}
.thumb-zone:hover,.thumb-zone.drag{border-color:#666;background:#222}
.thumb-zone input{position:absolute;inset:0;opacity:0;cursor:pointer}
.thumb-zone .icon{font-size:28px;margin-bottom:8px}
.thumb-zone p{font-size:13px;color:#666}
.thumb-preview{width:100%;border-radius:8px;margin-top:12px;display:none}
.row{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.btn{width:100%;padding:13px;background:#fff;color:#000;border:none;border-radius:10px;font-size:15px;font-weight:600;cursor:pointer;transition:opacity .15s;margin-top:8px}
.btn:hover{opacity:.85}
.btn:disabled{opacity:.4;cursor:not-allowed}
.progress-wrap{display:none;margin-bottom:16px}
.bar-bg{background:#2a2a2a;border-radius:999px;height:6px;overflow:hidden;margin-bottom:8px}
.bar{background:#fff;height:100%;border-radius:999px;transition:width .4s;width:0%}
.pct{font-size:12px;color:#666;text-align:right}
.log-box{background:#111;border:1px solid #222;border-radius:8px;padding:12px;max-height:180px;overflow-y:auto;font-size:12px;font-family:monospace;color:#aaa;line-height:1.7}
.results{display:none}
.clip-card{background:#1a1a1a;border:1px solid #2a2a2a;border-radius:12px;padding:16px;margin-bottom:10px;display:flex;align-items:center;gap:14px}
.clip-num{width:36px;height:36px;border-radius:50%;background:#222;display:flex;align-items:center;justify-content:center;font-size:13px;font-weight:600;flex-shrink:0}
.clip-info{flex:1;min-width:0}
.clip-title{font-size:14px;font-weight:500;margin-bottom:3px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.clip-meta{font-size:12px;color:#666}
.dl-btn{background:#fff;color:#000;border:none;border-radius:8px;padding:8px 16px;font-size:13px;font-weight:600;cursor:pointer;white-space:nowrap;flex-shrink:0}
.dl-btn:hover{opacity:.8}
.error-box{background:#2a1010;border:1px solid #5a2020;border-radius:8px;padding:12px;font-size:13px;color:#f88;display:none;margin-bottom:12px}
</style>
</head>
<body>
<div class="wrap">
  <h1>ショート動画 自動生成</h1>
  <p class="sub">YouTubeのURLとサムネイルを入れるだけで30〜40秒のショート動画を作ります</p>

  <div class="card">
    <h2>動画情報</h2>
    <label>YouTube URL</label>
    <input type="url" id="url" placeholder="https://www.youtube.com/watch?v=..." />
    <label>チャンネル名</label>
    <input type="text" id="channel" placeholder="例: クリニックマーケのプロ" />
    <label>タイトルテキスト（上部に表示）</label>
    <input type="text" id="title" placeholder="例: 看板広告ってホントに必要！！？" />
    <div class="row" style="margin-top:14px">
      <div>
        <label style="margin-top:0">生成するクリップ数</label>
        <input type="number" id="clips" value="3" min="1" max="5" />
      </div>
    </div>
    <label>1クリップの長さ</label>
    <div class="slider-wrap">
      <input type="range" id="durSlider" min="15" max="120" value="35" oninput="syncDur('slider')">
      <span class="slider-val" id="durLabel">35秒</span>
    </div>
    <div class="dur-row">
      <label style="color:#666;font-size:12px">直接入力（秒）:</label>
      <input type="number" id="durInput" value="35" min="1" style="width:90px" oninput="syncDur('input')">
    </div>
    <label>切り抜きの指示（任意）</label>
    <textarea id="instruction" placeholder="例: 一番盛り上がる部分／結論を話しているシーン／笑えるところ"></textarea>
  </div>

  <div class="card">
    <h2>サムネイル画像</h2>
    <div class="thumb-zone" id="thumbZone">
      <input type="file" id="thumbFile" accept="image/*" onchange="previewThumb(this)">
      <div class="icon">🖼</div>
      <p>クリックまたはドラッグ＆ドロップ</p>
      <p style="margin-top:4px;font-size:11px;color:#444">JPG / PNG / WEBP</p>
    </div>
    <img class="thumb-preview" id="thumbPreview" />
  </div>

  <div class="error-box" id="errorBox"></div>

  <div class="progress-wrap" id="progressWrap">
    <div class="card">
      <h2>処理中</h2>
      <div class="bar-bg"><div class="bar" id="bar"></div></div>
      <div class="pct" id="pct">0%</div>
      <div class="log-box" id="logBox"></div>
    </div>
  </div>

  <div class="results" id="resultsWrap">
    <div class="card">
      <h2>完成！ダウンロード</h2>
      <div id="clipList"></div>
    </div>
  </div>

  <button class="btn" id="runBtn" onclick="startJob()">生成開始</button>
</div>

<script>
let pollTimer = null;
function previewThumb(input) {
  const file = input.files[0];
  if (!file) return;
  const reader = new FileReader();
  reader.onload = e => {
    const img = document.getElementById('thumbPreview');
    img.src = e.target.result;
    img.style.display = 'block';
  };
  reader.readAsDataURL(file);
}
function syncDur(src) {
  const slider = document.getElementById('durSlider');
  const input  = document.getElementById('durInput');
  const label  = document.getElementById('durLabel');
  if (src === 'slider') {
    input.value = slider.value;
  } else {
    const v = Math.max(1, parseInt(input.value) || 35);
    input.value = v;
    slider.value = Math.min(120, v);
  }
  label.textContent = (parseInt(input.value) || 35) + '秒';
}
async function startJob() {
  const url         = document.getElementById('url').value.trim();
  const channel     = document.getElementById('channel').value.trim() || 'チャンネル名';
  const title       = document.getElementById('title').value.trim() || 'タイトル';
  const clips       = parseInt(document.getElementById('clips').value) || 3;
  const clipDur     = parseInt(document.getElementById('durInput').value) || 35;
  const instruction = document.getElementById('instruction').value.trim();
  const file        = document.getElementById('thumbFile').files[0];
  const err = document.getElementById('errorBox');
  err.style.display = 'none';
  if (!url) { showError('YouTubeのURLを入力してください'); return; }
  if (!file) { showError('サムネイル画像を選択してください'); return; }
  document.getElementById('runBtn').disabled = true;
  document.getElementById('progressWrap').style.display = 'block';
  document.getElementById('resultsWrap').style.display = 'none';
  document.getElementById('logBox').textContent = '';
  const fd = new FormData();
  fd.append('youtube_url', url);
  fd.append('channel', channel);
  fd.append('title', title);
  fd.append('num_clips', clips);
  fd.append('clip_duration', clipDur);
  fd.append('instruction', instruction);
  fd.append('thumbnail', file);
  try {
    const res = await fetch('/api/generate', { method: 'POST', body: fd });
    const data = await res.json();
    if (data.error) { showError(data.error); return; }
    pollStatus(data.job_id);
  } catch(e) {
    showError('サーバーへの接続に失敗しました: ' + e.message);
  }
}
function pollStatus(jobId) {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(async () => {
    try {
      const res  = await fetch('/api/status/' + jobId);
      const data = await res.json();
      const logBox = document.getElementById('logBox');
      logBox.textContent = data.logs.join('\\n');
      logBox.scrollTop = logBox.scrollHeight;
      const pct = Math.max(0, data.progress);
      document.getElementById('bar').style.width = pct + '%';
      document.getElementById('pct').textContent = pct + '%';
      if (data.status === 'done') {
        clearInterval(pollTimer);
        showResults(data.results);
        document.getElementById('runBtn').disabled = false;
      } else if (data.status === 'error') {
        clearInterval(pollTimer);
        showError(data.error || 'エラーが発生しました');
        document.getElementById('runBtn').disabled = false;
      }
    } catch(e) {}
  }, 1500);
}
function showResults(results) {
  const wrap = document.getElementById('resultsWrap');
  const list = document.getElementById('clipList');
  list.innerHTML = results.map(c => `
    <div class="clip-card">
      <div class="clip-num">${c.rank}</div>
      <div class="clip-info">
        <div class="clip-title">${c.title}</div>
        <div class="clip-meta">${fmt(c.start_seconds)} → ${fmt(c.end_seconds)} · ${c.size_mb}MB</div>
        <div class="clip-meta" style="margin-top:2px;color:#555">${c.reason || ''}</div>
      </div>
      <button class="dl-btn" onclick="location.href='${c.download_url}'">↓ 保存</button>
    </div>
  `).join('');
  wrap.style.display = 'block';
}
function fmt(s) {
  s = Math.floor(s);
  return String(Math.floor(s/60)).padStart(2,'0') + ':' + String(s%60).padStart(2,'0');
}
function showError(msg) {
  const el = document.getElementById('errorBox');
  el.textContent = '❌ ' + msg;
  el.style.display = 'block';
  document.getElementById('runBtn').disabled = false;
  document.getElementById('progressWrap').style.display = 'none';
}
const zone = document.getElementById('thumbZone');
zone.addEventListener('dragover', e => { e.preventDefault(); zone.classList.add('drag'); });
zone.addEventListener('dragleave', () => zone.classList.remove('drag'));
zone.addEventListener('drop', e => {
  e.preventDefault();
  zone.classList.remove('drag');
  const file = e.dataTransfer.files[0];
  if (file && file.type.startsWith('image/')) {
    document.getElementById('thumbFile').files = e.dataTransfer.files;
    previewThumb(document.getElementById('thumbFile'));
  }
});
</script>
</body>
</html>"""

if __name__ == "__main__":
    missing = []
    for tool in ["yt-dlp", "ffmpeg"]:
        if subprocess.run(["which", tool], capture_output=True).returncode != 0:
            missing.append(tool)
    try: import anthropic
    except ImportError: missing.append("anthropic  →  pip install anthropic fastapi uvicorn python-multipart")
    if missing:
        print("❌ 以下が必要です:")
        for m in missing: print(f"   {m}")
        sys.exit(1)
    if not ANTHROPIC_API_KEY:
        print("❌ ANTHROPIC_API_KEY が設定されていません")
        print("   export ANTHROPIC_API_KEY='sk-ant-...'")
        sys.exit(1)
    print("🎬 起動中... http://localhost:8000 をブラウザで開いてください（停止: Ctrl+C）")
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="warning")
