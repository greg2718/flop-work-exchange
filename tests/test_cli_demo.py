from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from flop_work_exchange.cli import main
from flop_work_exchange.receipts import verify_receipt


def test_module_demo_writes_verifiable_receipt(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["demo", "--state-dir", str(tmp_path)])
    captured = capsys.readouterr()
    assert code == 0
    payload = json.loads(captured.out)
    assert payload["ok"] is True
    assert payload["receipt"]["payment_mode"] == "paper"
    assert payload["receipt"]["settlement_status"] == "simulated"
    assert payload["verification"]["ok"] is True
    receipt_path = Path(payload["receipt_path"])
    assert receipt_path.exists()
    file_payload = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert verify_receipt(file_payload)["ok"] is True
    for field in (
        "job_id",
        "buyer_did",
        "seller_did",
        "operator_relationship",
        "service",
        "price_flop",
        "payment_mode",
        "tclk_deal_id",
        "result_hash",
        "bench_result",
        "completed_at",
        "settlement_status",
    ):
        assert field in file_payload


def test_cli_list_jobs_and_verify_receipt(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["demo", "--state-dir", str(tmp_path)]) == 0
    capsys.readouterr()
    assert main(["--state-dir", str(tmp_path), "list-jobs"]) == 0
    jobs = json.loads(capsys.readouterr().out)
    assert jobs[0]["status"] == "COMPLETED"
    job_id = jobs[0]["job_id"]
    receipt_file = tmp_path / "receipts" / f"{job_id}.json"
    assert main(["verify-receipt", str(receipt_file)]) == 0
    verification = json.loads(capsys.readouterr().out)
    assert verification["ok"] is True


def test_cli_doctor_and_live_demo(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["doctor"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] is True
    assert report["payment_mode"] == "paper"
    assert report["adapter_modes"]["scout"] == "stub"
    assert main(["live-demo", "--state-dir", str(tmp_path)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["receipt"]["payment_mode"] == "paper"
    assert payload["adapter_kinds"]["scout"] == "stub"


def test_cli_doctor_reads_yaml_config(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    for key in list(os.environ):
        if key.startswith("FLOP_WX_") or key == "FLOP_SCOUT_STATE_DIR":
            monkeypatch.delenv(key, raising=False)
    yaml_path = tmp_path / "ops.yaml"
    yaml_path.write_text(
        """
payment_mode: paper
adapters:
  scout_mode: local
  bench_mode: stub
  router_mode: stub
  sentinel_mode: stub
""",
        encoding="utf-8",
    )
    assert main(["--config", str(yaml_path), "doctor"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["adapter_modes"]["scout"] == "local"
    assert report["adapter_modes"]["bench"] == "stub"
    assert report["config_path"] == str(yaml_path)

    monkeypatch.setenv("FLOP_WX_SCOUT_MODE", "stub")
    assert main(["--config", str(yaml_path), "doctor"]) == 0
    overridden = json.loads(capsys.readouterr().out)
    assert overridden["adapter_modes"]["scout"] == "stub"


def test_cli_live_demo_nonzero_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        "flop_work_exchange.cli.run_live_demo",
        lambda *_args, **_kwargs: {
            "ok": False,
            "verification": {"ok": True},
            "adapter_errors": ["scout: warehouse exploded"],
        },
    )
    assert main(["live-demo", "--state-dir", str(tmp_path)]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False


def test_help_lists_required_commands() -> None:
    import io
    from contextlib import redirect_stdout

    buffer = io.StringIO()
    try:
        with redirect_stdout(buffer):
            main(["--help"])
    except SystemExit as exc:
        assert exc.code == 0
    text = buffer.getvalue()
    for command in (
        "post-job",
        "list-jobs",
        "submit-offer",
        "accept-offer",
        "submit-result",
        "verify",
        "settle",
        "show-receipt",
        "demo",
        "doctor",
        "live-demo",
    ):
        assert command in text
