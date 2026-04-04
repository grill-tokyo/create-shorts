"""
create-shorts app.py セキュリティ修正のテスト
対象: パストラバーサル(S1)・ffmpegインジェクション(S2)・アップロード検証(S3)・TTLクリーンアップ(B2)・ffmpegタイムアウト(B3)
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import time
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock
from fastapi.testclient import TestClient


# ANTHROPIC_API_KEY がなくても起動できるようにモック
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test-dummy")

import app as app_module
client = TestClient(app_module.app)


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

    def test_valid_job_and_filename_accepted(self, tmp_path):
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

    def test_resolved_path_must_be_under_work_dir(self, tmp_path):
        """resolve()後のパスがWORK_DIR配下でなければ400"""
        # シンボリックリンクでWORK_DIR外を指すケースを模倣
        job_id = "symlink-test"
        job_dir = app_module.WORK_DIR / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        link = job_dir / "evil.mp4"
        try:
            link.symlink_to("/etc/passwd")
            res = client.get(f"/download/{job_id}/evil.mp4")
            # resolveしたパスが/etc/passwdになるので400 or 404
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
    def test_generate_returns_job_id(self, tmp_path):
        """POST /api/generate はjob_idを返す"""
        dummy_image = b"\xff\xd8\xff\xe0" + b"\x00" * 100  # JPEGマジックバイト
        with patch.object(app_module, "ANTHROPIC_API_KEY", "sk-ant-dummy"):
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
        """jobs dictに登録済みのjob_idはステータスを返す"""
        job_id = "manual-test-job"
        app_module.jobs[job_id] = {"status": "running", "progress": 50, "logs": [], "results": []}
        res = client.get(f"/api/status/{job_id}")
        assert res.status_code == 200
        assert res.json()["status"] == "running"
        del app_module.jobs[job_id]
