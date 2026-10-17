import stat
from pathlib import Path

from unli_photos.engine.uploader import UploadResult
from unli_photos.photos.uploader import (
    CredRecord,
    CredsClient,
    GotophpUploader,
    parse_creds_list,
    parse_upload_output,
)


def write_script(path: Path, body: str) -> Path:
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)
    return path


class TestParseUploadOutput:
    def test_success_media_key(self):
        text = '{"success": true, "mediaKey": "MK_1", "deleteMarkdown": "..."}'
        media_key, err = parse_upload_output(text)
        assert media_key == "MK_1"
        assert err is None

    def test_error_field(self):
        text = '{"success": false, "error": "auth expired"}'
        media_key, err = parse_upload_output(text)
        assert media_key is None
        assert err == "auth expired"

    def test_message_field_fallback(self):
        _, err = parse_upload_output('{"message": "invalid token"}')
        assert err == "invalid token"

    def test_error_beats_message(self):
        _, err = parse_upload_output('{"error": "limit", "message": "details"}')
        assert err == "limit"

    def test_empty_output(self):
        assert parse_upload_output("") == (None, None)
        assert parse_upload_output("junk") == (None, None)


class TestGotophpUploader:
    def test_parse_album_key_from_success_output(self, tmp_path: Path):
        body = (
            '{"success": true, "mediaKey": "MK_9", '
            '"album": {"name": "P", "albumKeys": ["AF1QipXYZ"]}}'
        )
        stub = write_script(tmp_path / "fake-cli", f"echo '{body}'")
        up = GotophpUploader(binary=stub)
        result = up.upload(tmp_path / "p.jpg")
        assert result.ok is True
        assert result.album_key == "AF1QipXYZ"

    def test_no_album_key_when_output_lacks_it(self, tmp_path: Path):
        stub = write_script(
            tmp_path / "fake-cli", 'echo \'{"success": true, "mediaKey": "MK_9"}\''
        )
        up = GotophpUploader(binary=stub)
        result = up.upload(tmp_path / "p.jpg")
        assert result.ok is True
        assert result.album_key is None

    def test_upload_success_via_stub(self, tmp_path: Path):
        stub = write_script(tmp_path / "fake-cli", 'echo \'{"success": true, "mediaKey": "MK_9"}\'')
        cfg = tmp_path / "gotohp.config"
        cfg.touch()
        up = GotophpUploader(binary=stub, config_path=cfg)
        target = tmp_path / "photo.jpg"
        target.write_bytes(b"img")
        result = up.upload(target)
        assert result == UploadResult(ok=True, media_key="MK_9")

    def test_upload_failure_reports_error(self, tmp_path: Path):
        stub = write_script(
            tmp_path / "fake-cli", 'echo \'{"success": false, "error": "nope"}\'; exit 1'
        )
        up = GotophpUploader(binary=stub, config_path=None)
        target = tmp_path / "photo.jpg"
        target.write_bytes(b"img")
        result = up.upload(target)
        assert result.ok is False
        assert result.error == "nope"

    def test_upload_forwards_flags_and_config(self, tmp_path: Path):
        log = tmp_path / "args.log"
        stub = write_script(
            tmp_path / "fake-cli",
            f'printf "%s\\n" "$@" >> {log}; echo \'{{"success": true}}\'',
        )
        up = GotophpUploader(binary=stub, config_path=tmp_path / "cfg", account="me@x.io")
        up.upload(tmp_path / "p.jpg", force=True, timeout=5)
        content = log.read_text()
        assert "--no-tui" in content
        assert "--force" in content
        assert "--account" in content and "me@x.io" in content

    def test_upload_forwards_album_flag(self, tmp_path: Path):
        log = tmp_path / "args.log"
        stub = write_script(
            tmp_path / "fake-cli",
            f'printf "%s\\n" "$@" >> {log}; echo \'{{"success": true}}\'',
        )
        up = GotophpUploader(binary=stub)
        up.upload(tmp_path / "p.jpg", album="AUTO")
        content = log.read_text()
        assert "--album" in content and "AUTO" in content

    def test_upload_omits_album_when_unset(self, tmp_path: Path):
        log = tmp_path / "args.log"
        stub = write_script(
            tmp_path / "fake-cli",
            f'printf "%s\\n" "$@" >> {log}; echo \'{{"success": true}}\'',
        )
        up = GotophpUploader(binary=stub)
        up.upload(tmp_path / "p.jpg")
        assert "--album" not in log.read_text()

    def test_timeout_maps_to_failed_result(self, tmp_path: Path):
        stub = write_script(tmp_path / "fake-cli", "sleep 1")
        up = GotophpUploader(binary=stub)
        target = tmp_path / "photo.jpg"
        target.write_bytes(b"img")
        result = up.upload(target, timeout=0.1)
        assert result.ok is False
        assert "timeout" in (result.error or "")

    def test_config_hardened_after_call(self, tmp_path: Path):
        stub = write_script(tmp_path / "fake-cli", 'echo \'{"success": true, "mediaKey": "m"}\'')
        cfg = tmp_path / "gotohp.config"
        cfg.write_text("token")
        cfg.chmod(0o644)
        up = GotophpUploader(binary=stub, config_path=cfg)
        up.upload(tmp_path / "p.jpg")
        assert stat.S_IMODE(cfg.stat().st_mode) == 0o600


class TestParseCredsList:
    def test_active_marker_and_emails(self):
        text = "* bob@gmail.com (main)\nalice@somewhere.co.uk\nNo credentials found"
        records = parse_creds_list(text)
        assert records == [
            CredRecord(email="bob@gmail.com", active=True),
            CredRecord(email="alice@somewhere.co.uk", active=False),
        ]

    def test_empty_output(self):
        assert parse_creds_list("") == []
        assert parse_creds_list("No credentials found") == []


class TestCredsClient:
    def _stub(self, tmp_path: Path, list_out: str) -> tuple[CredsClient, Path, Path]:
        log = tmp_path / "creds_args.log"
        state = tmp_path / "creds_stdin.txt"
        stub = write_script(
            tmp_path / "fake-cli",
            'case "$3 $4" in\n'
            "  creds\\ list*) cat <<'LISTOUT'\n"
            f"{list_out}\n"
            "LISTOUT\n"
            "  ;;\n"
            f"  creds\\ add*) cat - > {state}\n"
            "  ;;\n"
            "esac\n"
            f'printf "%s\\n" "$@" >> {log}\n',
        )
        cfg = tmp_path / "gotohp.config"
        cfg.touch()
        client = CredsClient(binary=stub, config_path=cfg)
        return client, log, state

    def test_list_returns_records(self, tmp_path: Path):
        client, _, _ = self._stub(tmp_path, "* bob@x.io\nalice@y.io\n")
        assert client.list() == [
            CredRecord(email="bob@x.io", active=True),
            CredRecord(email="alice@y.io", active=False),
        ]

    def test_add_pipes_token_via_stdin(self, tmp_path: Path):
        client, _log, state = self._stub(tmp_path, "")
        client.add("SECRET_TOKEN_123")
        assert "SECRET_TOKEN_123" in state.read_text()

    def test_config_hardened(self, tmp_path: Path):
        client, _, _ = self._stub(tmp_path, "")
        cfg = Path(str(client._config_path))
        cfg.chmod(0o644)
        client.list()
        assert stat.S_IMODE(cfg.stat().st_mode) == 0o600
