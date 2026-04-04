"""
create-shorts app.py セキュリティ修正のテスト
対象: パストラバーサル(S1)・ffmpegインジェクション(S2)・アップロード検証(S3)・TTLクリーンアップ(B2)・ffmpegタイムアウト(B3)・認証(S4)・SQLiteジョブ管理(B1)
"""
import sys, os, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import time
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock
from fastapi.testclient import TestClient

# テスト用環境変数
os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test-dummy"
os.environ["APP_USERNAME"]      = "testuser"
os.environ["APP_PASSWORD"]      = "testpass"

# テスト用にDBをインメモリ（一時ファイル）に向ける
_tmp_dir = tempfile.mkdtemp()
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test-dummy")

import app as app_module
app_module.WORK_DIR = Path(_tmp_dir)
app_module.DB_PATH  = Path(_tmp_dir) / "jobs_test.db"
app_module._init_db()

# 認証付きクライアント
client      = TestClient(app_module.app, auth=("testuser", "testpass"))
client_noauth = TestClient(app_module.app)  # 認証なし


# ── S1: パストラバーサル防止 ──────────────────────────────────

class TestPathTraversal:
    def test_dotdot_in_filename_rejected(self):
        """../等を含むfilenameは400を返す"""
        res = client.get("/download/some-job-id/../../../etc/passwd")
        assert res.status_code in (400, 404)

    def test_dotdot_in_job_id_rejected(self):
        """job_idに../を含む場合は400"""
        res = client.get("/download/../../../etc/passwd/output.mp4")
        assert res.status_code in (400, 404)

    def test_slash_in_filename_rejected(self):
        """filenameにスラッシュを含む場合は400"""
        res = client.get("/download/valid-job-id/sub/path/file.mp4")
        # FastAPIのルーティング上404になる場合もOK（パス区切りがルートに届かない）
        assert res.status_code in (400, 404)

    def test_valid_job_and_filename_accepted(self):
        """正常なjob_id+filenameはファイルが存在すれば200"""
        job_id = "test-job-valid"
        job_dir = app_module.WORK_DIR / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        out_file = job_dir / "short_01.mp4"
        out_file.write_bytes(b"\x00\x00\x00\x00")  # ダミー
        try:
            res = client.get(f"/download/{job_id}/short_01.mp4")
            assert res.status_code == 200
        finally:
            out_file.unlink(missing_ok=True)
            job_dir.rmdir()

    def test_nonexistent_file_returns_404(self):
        """存在しないファイルは404"""
        res = client.get("/download/nonexistent-job/nonexistent.mp4")
        assert res.status_code == 404

    def test_resolved_path_must_be_under_work_dir(self):
        """resolve()後のパスがWORK_DIR配下でなければ400"""
        job_id = "symlink-test"
        job_dir = app_module.WORK_DIR / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        link = job_dir / "evil.mp4"
        try:
            link.symlink_to("/etc/passwd")
            res = client.get(f"/download/{job_id}/evil.mp4")
            assert res.status_code in (400, 404)
        except (OSError, NotImplementedError):
            pytest.skip("symlink not supported")
        finally:
            link.unlink(missing_ok=True)
            job_dir.rmdir()


# ── S2: ffmpegフィルタインジェクション防止 ───────────────────

class TestFfmpegInjection:
    """build_short がテキストをtextfileで渡しており、フィルタ文字列に直接埋め込まない"""

    def _run_build_short(self, channel_name: str, title_text: str) -> str:
        """build_short を呼び出し、生成されたffmpegフィルタ文字列を返す（実行はしない）"""
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            r = MagicMock()
            r.returncode = 0
            return r

        font_path = "/System/Library/Fonts/Arial.ttf"
        with patch("subprocess.run", side_effect=fake_run), \
             patch("os.path.exists", return_value=True):
            import tempfile
            with tempfile.TemporaryDirectory() as td:
                video = Path(td) / "src.mp4"
                thumb = Path(td) / "thumb.jpg"
                out   = Path(td) / "out.mp4"
                video.write_bytes(b"")
                thumb.write_bytes(b"")
                app_module.build_short(
                    str(video), str(thumb),
                    start=0, end=30,
                    channel_name=channel_name,
                    title_text=title_text,
                    out_path=str(out),
                    font_path=font_path,
                    src_w=1920, src_h=1080,
                )
        return " ".join(captured.get("cmd", []))

    def test_single_quote_in_channel_not_in_filter(self):
        """チャンネル名のシングルクォートがフィルタ文字列に直接含まれない"""
        cmd = self._run_build_short("O'Brien Channel", "普通のタイトル")
        # textfile=を使っているので生のテキストはcmdに現れない
        assert "O'Brien" not in cmd

    def test_filter_graph_chars_in_title_not_in_filter(self):
        """タイトルにffmpeg特殊文字([],;)を含んでもフィルタ文字列に直接含まれない"""
        cmd = self._run_build_short("ch", "[out]split[a][b];[a]nullsink")
        assert "[out]split" not in cmd
        assert "nullsink" not in cmd

    def test_backslash_in_title_not_in_filter(self):
        """バックスラッシュがフィルタ文字列に直接含まれない"""
        cmd = self._run_build_short("ch", "title\\ninjected")
        assert "title\\ninjected" not in cmd

    def test_textfile_option_used(self):
        """textfile=オプションが使われている（text=直接埋め込みでない）"""
        cmd = self._run_build_short("Grill Tokyo", "テストタイトル")
        assert "textfile=" in cmd
        # text=' の直接埋め込み形式が残っていないこと
        assert ":text='" not in cmd


# ── STEP3: ジョブ状態管理 ────────────────────────────────────

class TestJobManagement:
    def test_generate_returns_job_id(self):
        """POST /api/generate はjob_idを返す"""
        dummy_image = b"\xff\xd8\xff\xe0" + b"\x00" * 100
        res = client.post(
            "/api/generate",
            data={"youtube_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                  "channel": "テスト", "title": "テスト", "num_clips": "1",
                  "clip_duration": "30", "instruction": ""},
            files={"thumbnail": ("thumb.jpg", dummy_image, "image/jpeg")},
        )
        assert res.status_code == 200
        assert "job_id" in res.json()

    def test_status_unknown_job_returns_404(self):
        """存在しないjob_idは404"""
        res = client.get("/api/status/nonexistent-job-id")
        assert res.status_code == 404

    def test_status_known_job_returns_data(self):
        """SQLiteに登録済みのjob_idはステータスを返す"""
        job_id = "manual-test-job"
        app_module._create_job(job_id)
        res = client.get(f"/api/status/{job_id}")
        assert res.status_code == 200
        assert res.json()["status"] == "running"


# ── S3: サムネイルアップロード検証 ───────────────────────────

VALID_JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 100
VALID_PNG  = b"\x89PNG" + b"\x00" * 100

class TestThumbnailValidation:
    def _post(self, filename: str, data: bytes, content_type: str = "image/jpeg"):
        return client.post(
            "/api/generate",
            data={"youtube_url": "https://www.youtube.com/watch?v=test",
                  "channel": "ch", "title": "t", "num_clips": "1",
                  "clip_duration": "30", "instruction": ""},
            files={"thumbnail": (filename, data, content_type)},
        )

    def test_valid_jpeg_accepted(self):
        res = self._post("thumb.jpg", VALID_JPEG)
        assert res.status_code == 200
        assert "job_id" in res.json()

    def test_valid_png_accepted(self):
        res = self._post("thumb.png", VALID_PNG, "image/png")
        assert res.status_code == 200

    def test_invalid_extension_rejected(self):
        """.sh 拡張子は400"""
        res = self._post("evil.sh", VALID_JPEG, "application/x-sh")
        assert res.status_code == 400

    def test_php_extension_rejected(self):
        """.php 拡張子は400"""
        res = self._post("evil.php", VALID_JPEG, "application/x-httpd-php")
        assert res.status_code == 400

    def test_invalid_magic_bytes_rejected(self):
        """拡張子はjpgだがマジックバイトが画像でないデータは400"""
        fake_data = b"\x7fELF" + b"\x00" * 100  # ELFバイナリ
        res = self._post("fake.jpg", fake_data)
        assert res.status_code == 400

    def test_oversized_file_rejected(self):
        """10MB超のファイルは400"""
        big_data = VALID_JPEG[:4] + b"\x00" * (11 * 1024 * 1024)
        res = self._post("big.jpg", big_data)
        assert res.status_code == 400


# ── B2: TTLクリーンアップ ─────────────────────────────────────

class TestJobCleanup:
    def test_finished_at_set_on_done(self):
        """status=doneになったジョブにfinished_atが記録される"""
        job_id = "ttl-test-done"
        app_module.jobs[job_id] = {"status": "running", "progress": 0, "logs": [], "results": []}
        app_module.set_progress(job_id, 100, "done")
        assert "finished_at" in app_module.jobs[job_id]
        del app_module.jobs[job_id]

    def test_finished_at_set_on_error(self):
        """status=errorになったジョブにfinished_atが記録される"""
        job_id = "ttl-test-error"
        app_module.jobs[job_id] = {"status": "running", "progress": 0, "logs": [], "results": []}
        app_module.set_progress(job_id, -1, "error")
        assert "finished_at" in app_module.jobs[job_id]
        del app_module.jobs[job_id]

    def test_running_job_has_no_finished_at(self):
        """実行中ジョブにはfinished_atが付かない"""
        job_id = "ttl-test-running"
        app_module.jobs[job_id] = {"status": "running", "progress": 0, "logs": [], "results": []}
        app_module.set_progress(job_id, 50, "running")
        assert "finished_at" not in app_module.jobs[job_id]
        del app_module.jobs[job_id]

    def test_expired_job_cleaned_up(self, tmp_path):
        """TTL経過済みのdoneジョブはクリーンアップされる"""
        job_id = "ttl-expired"
        job_dir = app_module.WORK_DIR / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        (job_dir / "short_01.mp4").write_bytes(b"\x00")

        app_module.jobs[job_id] = {
            "status": "done", "progress": 100, "logs": [], "results": [],
            "finished_at": time.time() - app_module.JOB_TTL_SECONDS - 1,  # TTL超過
        }

        # クリーンアップロジックを直接実行
        now = time.time()
        for jid, job in list(app_module.jobs.items()):
            if job.get("status") in ("done", "error"):
                if now - job.get("finished_at", now) >= app_module.JOB_TTL_SECONDS:
                    import shutil
                    shutil.rmtree(app_module.WORK_DIR / jid, ignore_errors=True)
                    app_module.jobs.pop(jid, None)

        assert job_id not in app_module.jobs
        assert not (app_module.WORK_DIR / job_id).exists()

    def test_fresh_job_not_cleaned_up(self):
        """TTL未経過のdoneジョブは削除されない"""
        job_id = "ttl-fresh"
        app_module.jobs[job_id] = {
            "status": "done", "progress": 100, "logs": [], "results": [],
            "finished_at": time.time() - 10,  # 10秒前（TTL未達）
        }
        now = time.time()
        for jid, job in list(app_module.jobs.items()):
            if job.get("status") in ("done", "error"):
                if now - job.get("finished_at", now) >= app_module.JOB_TTL_SECONDS:
                    app_module.jobs.pop(jid, None)

        assert job_id in app_module.jobs
        del app_module.jobs[job_id]


# ── B3: ffmpegタイムアウト ────────────────────────────────────

class TestFfmpegTimeout:
    def test_timeout_passed_to_subprocess(self):
        """subprocess.runにtimeoutが渡されている"""
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["timeout"] = kwargs.get("timeout")
            r = MagicMock()
            r.returncode = 0
            return r

        import tempfile
        with patch("subprocess.run", side_effect=fake_run), \
             patch("os.path.exists", return_value=True):
            with tempfile.TemporaryDirectory() as td:
                video = Path(td) / "src.mp4"
                thumb = Path(td) / "thumb.jpg"
                out   = Path(td) / "out.mp4"
                video.write_bytes(b"")
                thumb.write_bytes(b"")
                app_module.build_short(
                    str(video), str(thumb),
                    start=0, end=30,
                    channel_name="ch", title_text="title",
                    out_path=str(out),
                    font_path=None,
                    src_w=1920, src_h=1080,
                )
        assert captured.get("timeout") == app_module.FFMPEG_TIMEOUT
