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

    def test_expired_job_cleaned_up(self):
        """TTL経過済みのdoneジョブはクリーンアップされる"""
        job_id = "ttl-expired"
        job_dir = app_module.WORK_DIR / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        (job_dir / "short_01.mp4").write_bytes(b"\x00")

        app_module._create_job(job_id)
        expired_at = time.time() - app_module.JOB_TTL_SECONDS - 1
        with app_module._db_lock, app_module._get_conn() as conn:
            conn.execute("UPDATE jobs SET status='done', finished_at=? WHERE job_id=?",
                         (expired_at, job_id))

        # クリーンアップロジックを直接実行
        now = time.time()
        with app_module._db_lock, app_module._get_conn() as conn:
            rows = conn.execute(
                "SELECT job_id, finished_at FROM jobs WHERE status IN ('done','error') AND finished_at IS NOT NULL"
            ).fetchall()
        for row in rows:
            if now - row["finished_at"] >= app_module.JOB_TTL_SECONDS:
                shutil.rmtree(app_module.WORK_DIR / row["job_id"], ignore_errors=True)
                with app_module._db_lock, app_module._get_conn() as conn:
                    conn.execute("DELETE FROM jobs WHERE job_id=?", (row["job_id"],))

        assert app_module._get_job(job_id) is None
        assert not (app_module.WORK_DIR / job_id).exists()

    def test_fresh_job_not_cleaned_up(self):
        """TTL未経過のdoneジョブは削除されない"""
        job_id = "ttl-fresh"
        app_module._create_job(job_id)
        fresh_at = time.time() - 10  # 10秒前（TTL未達）
        with app_module._db_lock, app_module._get_conn() as conn:
            conn.execute("UPDATE jobs SET status='done', finished_at=? WHERE job_id=?",
                         (fresh_at, job_id))

        now = time.time()
        with app_module._db_lock, app_module._get_conn() as conn:
            rows = conn.execute(
                "SELECT job_id, finished_at FROM jobs WHERE status IN ('done','error') AND finished_at IS NOT NULL"
            ).fetchall()
        for row in rows:
            if row["job_id"] == job_id and now - row["finished_at"] >= app_module.JOB_TTL_SECONDS:
                with app_module._db_lock, app_module._get_conn() as conn:
                    conn.execute("DELETE FROM jobs WHERE job_id=?", (row["job_id"],))

        assert app_module._get_job(job_id) is not None


# ── S4: Basic認証 ────────────────────────────────────────────

class TestAuthentication:
    def test_no_auth_returns_401(self):
        """認証なしアクセスは401"""
        res = client_noauth.get("/")
        assert res.status_code == 401

    def test_wrong_password_returns_401(self):
        """パスワード誤りは401"""
        bad = TestClient(app_module.app, auth=("testuser", "wrongpass"))
        res = bad.get("/")
        assert res.status_code == 401

    def test_correct_auth_returns_200(self):
        """正しい認証は200"""
        res = client.get("/")
        assert res.status_code == 200

    def test_api_generate_no_auth_returns_401(self):
        """/api/generate も認証必須"""
        dummy = b"\xff\xd8\xff\xe0" + b"\x00" * 100
        res = client_noauth.post(
            "/api/generate",
            data={"youtube_url": "https://www.youtube.com/watch?v=test",
                  "channel": "ch", "title": "t", "num_clips": "1",
                  "clip_duration": "30", "instruction": ""},
            files={"thumbnail": ("t.jpg", dummy, "image/jpeg")},
        )
        assert res.status_code == 401

    def test_api_status_no_auth_returns_401(self):
        """/api/status も認証必須"""
        res = client_noauth.get("/api/status/some-job")
        assert res.status_code == 401

    def test_download_no_auth_returns_401(self):
        """/download も認証必須"""
        res = client_noauth.get("/download/some-job/file.mp4")
        assert res.status_code == 401


# ── B1: SQLiteジョブ管理 ─────────────────────────────────────

class TestSQLiteJobManagement:
    def test_create_and_get_job(self):
        """ジョブ作成後にgetできる"""
        job_id = "sqlite-test-1"
        app_module._create_job(job_id)
        job = app_module._get_job(job_id)
        assert job is not None
        assert job["status"] == "running"
        assert job["progress"] == 0
        assert job["logs"] == []

    def test_log_appends_to_db(self):
        """log()がSQLiteに追記される"""
        job_id = "sqlite-test-2"
        app_module._create_job(job_id)
        app_module.log(job_id, "step1")
        app_module.log(job_id, "step2")
        job = app_module._get_job(job_id)
        assert "step1" in job["logs"]
        assert "step2" in job["logs"]

    def test_set_progress_updates_db(self):
        """set_progress()がSQLiteを更新する"""
        job_id = "sqlite-test-3"
        app_module._create_job(job_id)
        app_module.set_progress(job_id, 50, "running")
        job = app_module._get_job(job_id)
        assert job["progress"] == 50
        assert job["status"] == "running"

    def test_done_sets_finished_at(self):
        """status=doneでfinished_atが記録される"""
        job_id = "sqlite-test-4"
        app_module._create_job(job_id)
        app_module.set_progress(job_id, 100, "done")
        job = app_module._get_job(job_id)
        assert job["finished_at"] is not None

    def test_set_results_persisted(self):
        """_set_results()の結果がSQLiteから取得できる"""
        job_id = "sqlite-test-5"
        app_module._create_job(job_id)
        results = [{"rank": 1, "filename": "short_01.mp4", "size_mb": 5.2}]
        app_module._set_results(job_id, results)
        job = app_module._get_job(job_id)
        assert job["results"][0]["filename"] == "short_01.mp4"

    def test_nonexistent_job_returns_none(self):
        """存在しないjob_idはNoneを返す"""
        assert app_module._get_job("does-not-exist-xyz") is None


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
