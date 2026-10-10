import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import scout


def funded_task(**changes):
    result = {
        "task_id": "task_fixture",
        "title": "Fix a small Python parsing bug",
        "description": "Only use a public repository, no paid services.",
        "status": "open",
        "bounty_token": "USDC",
        "bounty_network": "eip155:8453",
        "bounty": {"token": "USDC", "amount_display": "2.00"},
        "escrow": {
            "status": "funded",
            "deposit_tx_hash": "0x" + "c" * 64,
            "funded_at": "2026-10-10T12:00:00Z",
        },
    }
    result.update(changes)
    return result


class ClassificationTests(unittest.TestCase):
    def check(self, source, item, expected, **kwargs):
        category, reasons, proof = scout.classify(source, item, **kwargs)
        self.assertEqual(category, expected, reasons)
        self.assertIsInstance(reasons, list)
        self.assertFalse(proof["age_eligibility_verified"])

    def test_positive_funding_needs_base_escrow_and_proof(self):
        self.check("basedagents", funded_task(), "real-funded")
        self.check("basedagents", funded_task(escrow={"status": "funded"}), "declared-not-funded")
        self.check("basedagents", funded_task(escrow={"status": "pending", "deposit_tx_hash": "0x" + "c"*64}), "declared-not-funded")
        self.check("basedagents", funded_task(escrow=None, payment_verified=1), "declared-not-funded")
        self.check("basedagents", funded_task(bounty_network="eip155:84532"), "sandbox")

    def test_testing_and_spam_precedence_over_money(self):
        self.check("basedagents", funded_task(title="Test hire (please ignore)"), "unusable")
        self.check("basedagents", funded_task(description="Smoke test only"), "unusable")
        self.check("basedagents", funded_task(tags=["spam"]), "unusable")

    def test_spend_and_age(self):
        self.check("basedagents", funded_task(required_spend="0.01"), "unusable")
        self.check("basedagents", funded_task(description="Purchase credits before starting"), "unusable")
        self.check("basedagents", funded_task(conduct={"requiredSpendAtomic": "20000"}), "unusable")
        self.check("basedagents", funded_task(description="Applicants must be at least 18 years old."), "unusable")
        self.check("basedagents", funded_task(minAge=18), "unusable")
        self.check("basedagents", funded_task(status="closed"), "unusable")
        self.check("basedagents", funded_task(claimed_by_agent_id="someone"), "unusable")

    def test_brick_real_funds_not_inferred(self):
        paid = {"title": "Document API", "state": "open",
                "paymentState": "escrowed",
                "reward": {"display": "10.00", "asset": "USDC", "network": "sandbox"}}
        self.check("brick", paid, "sandbox")
        paid["reward"]["network"] = "base-mainnet"
        self.check("brick", paid, "declared-not-funded")
        paid["reward"]["display"] = "0"
        self.check("brick", paid, "declared-not-funded")

    def test_agentbounties_projection_not_funding_proof(self):
        opportunity = {
            "title": "Fix CLI", "work_state": "claimable", "payment_state": "escrowed",
            "source_type": "canonical_base", "network": "base-mainnet",
            "reward": {"amount_display": "20"}
        }
        self.check("agentbounties", opportunity, "unknown", feed_available=True)
        self.check("agentbounties", opportunity, "unknown", feed_available=False)
        self.check("agentbounties", dict(opportunity, payment_state="announced"), "declared-not-funded")

    def test_taskmarket_escrow_is_unverified_until_chain_check(self):
        task = {"id": "0x" + "a"*64, "description": "Fix small Python CLI bug",
                "status": "open", "reward": "5000000", "submissionCount": 0,
                "escrowTxHash": "0x" + "b"*64, "expiryTime": "2099-01-01T00:00:00Z"}
        self.check("taskmarket", task, "unknown")
        self.assertEqual(scout.parse_numeric_amount(task, "taskmarket"), "5")
        self.check("taskmarket", dict(task, escrowTxHash=None), "declared-not-funded")
        self.check("taskmarket", dict(task, submissionCount=4), "unusable")
        self.check("taskmarket", dict(task, stakeRequired=True), "unusable")
        self.check("taskmarket", dict(task, expiryTime="2020-01-01T00:00:00Z"), "unusable")
        self.check("taskmarket", dict(task, description="Swap crypto tokens now"), "unusable")
        self.check("taskmarket", dict(task, description="Agent self portrait original image"), "unusable")
        self.check("taskmarket", dict(task, submissionWindowOpen=False), "unusable")

    def test_failure_api_is_recorded_not_claimed_empty(self):
        def broken_fetch(url):
            if "/v1/status" in url:
                return {"status": "operational"}
            if "brick" in url:
                raise RuntimeError("HTTP 503")
            if "basedagents" in url:
                return {"ok": True, "tasks": [funded_task()]}
            if "taskmarket" in url:
                return {"tasks": []}
            return {"degraded": True, "source_statuses": [], "items": []}
        report = scout.scan("live", fetch=broken_fetch)
        self.assertFalse(report["sources"]["brick"]["ok"])
        self.assertIn("503", report["sources"]["brick"]["error"])
        self.assertEqual(report["counts"]["real-funded"], 1)
        self.assertFalse(report["sources"]["agentbounties"]["canonical_available"])
        with tempfile.TemporaryDirectory() as temp:
            paths = scout.write_reports(report, Path(temp))
            self.assertTrue(all(path.exists() for path in paths))
            self.assertEqual(json.loads(paths[0].read_text())["counts"]["real-funded"], 1)

    def test_html_is_not_api_and_bad_json_rejected(self):
        with patch.object(scout, "urlopen") as fake:
            response = fake.return_value.__enter__.return_value
            response.read.return_value = b"<!DOCTYPE html><html></html>"
            response.headers.get.return_value = "text/html"
            with self.assertRaises(RuntimeError):
                scout.pull_json("https://example.invalid/v1/tasks")

    def test_offline_end_to_end_and_csv(self):
        report = scout.scan("offline")
        self.assertEqual(report["counts"]["real-funded"], 1)
        self.assertEqual(report["counts"]["sandbox"], 1)
        self.assertGreaterEqual(report["counts"]["unusable"], 3)
        self.assertEqual(report["counts"]["unknown"], 2)
        self.assertFalse(report["eligible_for_minor_asserted"])
        with tempfile.TemporaryDirectory() as temp:
            _, md, csv_file = scout.write_reports(report, Path(temp))
            self.assertIn("DEGRADADO", md.read_text(encoding="utf-8"))
            import csv
            with csv_file.open(encoding="utf-8-sig", newline="") as fp:
                rows = list(csv.DictReader(fp))
            self.assertEqual(len(rows), len(report["rows"]))
            self.assertTrue(all(row["age_eligibility_verified"] == "False" for row in rows))


if __name__ == "__main__":
    unittest.main()
