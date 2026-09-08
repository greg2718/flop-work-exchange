from __future__ import annotations

import json
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
    ):
        assert command in text
