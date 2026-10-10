#!/usr/bin/env python3
"""Read-only public task scout. Python 3.9+, standard library only."""
import argparse
import csv
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import re
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "output"
FIXTURE = ROOT / "fixtures"
TIMEOUT_SECONDS = 8
MAX_BYTES = 2_000_000
ENDPOINTS = {
    "basedagents_status": "https://api.basedagents.ai/v1/status",
    "basedagents": "https://api.basedagents.ai/v1/tasks?status=open&limit=100",
    "brick": "https://brick.blue/api/v1/tasks",
    "taskmarket": "https://api.taskmarket.dev/api/tasks?status=open&limit=100",
    "agentbounties": (
        "https://api.agentbounties.app/v1/opportunities"
        "?limit=100&network=base-mainnet&source_type=canonical_base&view=ready_to_earn"
    ),
}
STATUSES = ("real-funded", "declared-not-funded", "sandbox", "unusable", "unknown")
TEST_RE = re.compile(
    r"\b(test(?:ing)?|sandbox|dummy|mock|fake|smoke[\s-]?test|"
    r"please\s+ignore|dry[\s-]?run|faucet|"
    r"trial(?:\s+task)?|example(?:\s+only)?|"
    r"benchmark|qa[\s-]?only)\b",
    re.I,
)
AGE_RE = re.compile(
    r"\b(?:18\s*\+|21\s*\+|over\s+18|"
    r"(?:minimum|at least|must be|age(?:d)?|older than)\s+(?:of\s+)?(?:age\s+)?1[89]\b|"
    r"(?:must\s+be|only)\s+(?:a\s+)?(?:legal\s+)?adult|"
    r"adults?\s+only|mayores?\s+de\s+18|18\s+(?:years\s+old|años))",
    re.I,
)
SPEND_RE = re.compile(
    r"\b(?:deposit\s+(?:to|first|before)|pay\s+(?:to|first|upfront)|"
    r"purchase\s+(?:a|an|the|credits?|tokens?)|"
    r"buy\s+(?:a|an|the|credits?|tokens?)|"
    r"stake\s+(?:some|a|your|to)|"
    r"registration\s+fee|entry\s+fee|"
    r"requir(?:e|es|ed)\s+(?:a\s+)?(?:payment|purchase|deposit|stake)|"
    r"gas\s+fee\s+(?:required|upfront))\b",
    re.I,
)
RISK_RE = re.compile(
    r"\b(?:swap\s+(?:\w+\s+){0,3}?(?:tokens?|crypto|coins?|usdc|eth)|"
    r"bridge\s+(?:\w+\s+){0,3}?(?:tokens?|crypto|coins?|usdc|eth)|"
    r"send\s+(?:\w+\s+){0,2}?(?:tokens?|coins?|usdc)|"
    r"distribut(?:e|ion)\s+(?:\w+\s+){0,2}?(?:tokens?|coins?)|"
    r"connect\s+(?:your\s+)?wallet\s+to\s+(?:claim|mint|swap)|"
    r"mint\s+(?:an?\s+)?nft)\b",
    re.I,
)
MAINNETS = {"eip155:8453", "base-mainnet", "base", "mainnet", "8453"}
TESTNETS = {"sandbox", "test", "testnet", "base-sepolia", "eip155:84532", "84532", "simulated"}


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def decimal_positive(value):
    if value is None or isinstance(value, bool):
        return False
    try:
        n = Decimal(str(value))
        return n.is_finite() and n > 0
    except (InvalidOperation, ValueError, TypeError):
        return False


def nonzero(value):
    return decimal_positive(value)


def as_dict(value):
    return value if isinstance(value, dict) else {}


def as_list(value):
    return value if isinstance(value, list) else []


def slug_text(value, max_length=300):
    return " ".join(str(value or "").split())[:max_length]


def status_alias(text):
    return slug_text(text, 50).lower().replace("_", "-")


def parse_numeric_amount(raw, source):
    data = as_dict(raw)
    if source == "basedagents":
        bounty = as_dict(data.get("bounty"))
        if decimal_positive(bounty.get("amount_display")):
            return str(bounty["amount_display"])
        # On BasedAgents, bounty_amount is an atomic quantity, not dollars.
        atom = bounty.get("amount_atomic") or data.get("bounty_amount")
        if decimal_positive(atom):
            return str(Decimal(str(atom)) / Decimal(1_000_000))
        return ""
    if source == "brick":
        reward = as_dict(data.get("reward"))
        return str(reward.get("display")) if decimal_positive(reward.get("display")) else ""
    if source == "taskmarket":
        # TaskMarket encodes USDC in 6-decimal atomic units.
        raw = data.get("reward")
        if decimal_positive(raw):
            return str(Decimal(str(raw)) / Decimal(1_000_000))
        return ""
    reward = as_dict(data.get("reward"))
    for key in ("amount_display", "display", "amount_usdc", "usd", "amountUsd"):
        if decimal_positive(reward.get(key)):
            return str(reward[key])
    for key in ("reward_usdc", "amount_usdc", "amountUsd"):
        if decimal_positive(data.get(key)):
            return str(data[key])
    return ""


def age_constraints(data, description):
    data = as_dict(data)
    for key in ("minimum_age", "minimumAge", "min_age", "minAge", "age_requirement"):
        raw = data.get(key)
        if decimal_positive(raw) and Decimal(str(raw)) >= 18:
            return True
    return bool(AGE_RE.search(description))


def spend_constraints(data, description):
    data = as_dict(data)
    for key in ("required_spend", "requiredSpend", "required_spend_usdc",
                "requiredSpendAtomic", "entry_fee", "entryFee", "required_deposit"):
        value = data.get(key)
        if isinstance(value, dict):
            value = value.get("amount") or value.get("amount_atomic")
        if nonzero(value):
            return True
    for parent in ("conduct", "requirements", "economics", "constraints"):
        values = as_dict(data.get(parent))
        for key in ("requiredSpend", "requiredSpendAtomic", "minSpendAtomic", "entryFee"):
            if nonzero(values.get(key)):
                return True
    return bool(SPEND_RE.search(description))


def classify(source, data, *, feed_available=True):
    """Return (category, reasons, evidence); never infer age eligibility."""
    data = as_dict(data)
    title = slug_text(data.get("title") or data.get("name"), 500)
    description = slug_text(data.get("description") or data.get("summary"), 15000)
    searchable = title + " " + description
    escrow = as_dict(data.get("escrow"))
    reward = as_dict(data.get("reward"))
    bounty = as_dict(data.get("bounty"))
    amount = parse_numeric_amount(data, source)
    network = status_alias(
        data.get("bounty_network")
        or bounty.get("network")
        or reward.get("network")
        or data.get("network")
    )
    payment = status_alias(data.get("paymentState") or data.get("payment_state")
                           or data.get("paymentStatus") or data.get("payment_status"))
    work_state = status_alias(data.get("work_state") or data.get("workState")
                              or data.get("status") or data.get("state"))
    escrow_status = status_alias(escrow.get("status"))
    evidence = {
        "amount_usdc": amount, "network": network,
        "payment_state": payment, "escrow_status": escrow_status,
        "deposit_tx_hash": slug_text(escrow.get("deposit_tx_hash") or
                                     data.get("escrowTxHash"), 100),
        "work_state": work_state, "source_level": "public API response",
        "age_eligibility_verified": False,
    }
    if network in TESTNETS:
        return "sandbox", ["Red sandbox/testnet: no equivale a USDC retirables en Base mainnet."], evidence
    if TEST_RE.search(searchable) or any(
        str(tag).lower() in ("test", "testing", "spam", "sandbox", "dummy", "trial")
        for tag in as_list(data.get("tags"))
    ):
        return "unusable", ["Tarea expresamente de prueba, sandbox, ejemplo o benchmark."], evidence
    deadline = data.get("expiryTime") or data.get("expires_at") or data.get("deadline")
    if deadline:
        try:
            parsed_deadline = datetime.fromisoformat(str(deadline).replace("Z", "+00:00"))
            if parsed_deadline.tzinfo is None:
                parsed_deadline = parsed_deadline.replace(tzinfo=timezone.utc)
            if parsed_deadline <= datetime.now(timezone.utc):
                return "unusable", ["Plazo de entrega vencido."], evidence
        except (ValueError, OverflowError):
            return "unknown", ["Fecha límite no interpretable."], evidence
    if source == "taskmarket":
        if data.get("submissionWindowOpen") is False:
            return "unusable", ["Ventana de entregas cerrada."], evidence
        if nonzero(data.get("submissionCount")) and Decimal(str(data["submissionCount"])) > 3:
            return "unusable", ["Alta competencia: más de 3 entregas registradas."], evidence
        if data.get("stakeRequired") is True or nonzero(data.get("stakeBps")):
            return "unusable", ["Requiere stake para participar."], evidence
        if data.get("hasAccessPassword") is True or data.get("taskVisibility") not in (None, "public"):
            return "unusable", ["No es una tarea pública sin credenciales."], evidence
        if RISK_RE.search(searchable):
            return "unusable", ["Operación cripto o movimiento de tokens solicitado."], evidence
        if re.search(r"\b(?:self[\s-]?portrait|original\s+image|artwork|paint(?:ing)?|illustrat(?:ion|e))\b", searchable, re.I):
            return "unusable", ["Encargo de arte/imagen, fuera del alcance de un agente de código autónomo."], evidence
    if age_constraints(data, description):
        return "unusable", ["La descripción exige una edad mínima de 18 años o más."], evidence
    if spend_constraints(data, description):
        return "unusable", ["Exige adelantar dinero, depósito, compra, stake o gasto."], evidence
    if work_state and work_state not in ("open", "claimable", "available", "ready"):
        return "unusable", ["Estado de trabajo no abierto: " + work_state], evidence
    if status_alias(data.get("claimed_by_agent_id") or data.get("claimed_by")) not in ("", "none", "null"):
        return "unusable", ["Ya asignada a otro agente."], evidence
    if not amount:
        return "declared-not-funded", ["Sin importe monetario positivo verificable en el registro público."], evidence
    if source == "basedagents":
        tx_hash = evidence["deposit_tx_hash"]
        valid_tx = bool(re.fullmatch(r"0x[a-fA-F0-9]{64}", tx_hash))
        if (network in MAINNETS and escrow_status == "funded" and valid_tx
                and escrow.get("refunded_at") is None and escrow.get("released_at") is None
                and str(bounty.get("token") or data.get("bounty_token") or "").upper() == "USDC"):
            return "real-funded", ["API BasedAgents afirma escrow funded Base mainnet y aporta hash de depósito; pendiente de confirmación independiente onchain y elegibilidad."], evidence
        return "declared-not-funded", ["Pago anunciado o pendiente; falta escrow funded con hash de depósito válido."], evidence
    if source == "brick":
        # Brick's published docs state rewards are escrowed at creation, but
        # no independently validated Base mainnet transfer is provided by
        # the list. A paid-looking sandbox row must never be promoted.
        return "declared-not-funded", [
            "Importe anunciado sin evidencia suficiente de custodia de fondos en Base mainnet; 'escrowed' por sí solo no basta."
        ], evidence
    if source == "taskmarket":
        tx_hash = evidence["deposit_tx_hash"]
        if not re.fullmatch(r"0x[a-fA-F0-9]{64}", tx_hash):
            return "declared-not-funded", ["Premio anunciado sin hash de depósito verificable."], evidence
        return "unknown", [
            "API pública publica escrowTxHash y premio en micro-USDC; "
            "falta corroborar en cadena que el depósito corresponda al trabajo, siga retenido y sea retirables."
        ], evidence
    if source == "agentbounties":
        if not feed_available:
            return "unknown", ["Fuente canónica degradada/no disponible: imposible verificar fondos."], evidence
        source_type = status_alias(data.get("source_type") or data.get("sourceType"))
        if (source_type not in ("canonical-base", "canonical-base-mainnet")
                or payment not in ("escrowed", "funded")
                or work_state not in ("claimable", "open")
                or network not in MAINNETS):
            return "declared-not-funded", ["No reúne fuente canónica, Base mainnet, estado claimable y pago escrowed."], evidence
        # A projected escrow state alone is not proof of canonical funding.
        return "unknown", ["Proyección pública afirma escrow; faltan pruebas canónicas independientes de financiación."], evidence
    return "unknown", ["Mercado o esquema desconocido."], evidence


def pull_json(url, timeout=TIMEOUT_SECONDS):
    request = Request(url, headers={
        "Accept": "application/json", "User-Agent": "paid-task-scout/1.0 (read-only; stdlib)"
    }, method="GET")
    try:
        with urlopen(request, timeout=timeout) as response:
            ctype = response.headers.get("Content-Type", "")
            body = response.read(MAX_BYTES + 1)
            if len(body) > MAX_BYTES:
                raise ValueError("Respuesta mayor que límite de 2 MB")
            if "html" in ctype.lower() or body.lstrip().startswith((b"<!DOCTYPE", b"<html")):
                raise ValueError("La URL devolvió HTML, no JSON API")
            obj = json.loads(body.decode("utf-8"))
            if not isinstance(obj, (dict, list)):
                raise ValueError("Respuesta JSON sin objeto/lista")
            return obj
    except (HTTPError, URLError, OSError, TimeoutError, ValueError,
            UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(type(exc).__name__ + ": " + slug_text(exc, 200)) from exc


def fixtures():
    return {key: json.loads((FIXTURE / (key + ".json")).read_text(encoding="utf-8"))
            for key in ENDPOINTS}


def source_rows(source, data):
    if source == "basedagents":
        if not isinstance(data, dict) or not isinstance(data.get("tasks"), list):
            raise ValueError("BasedAgents: falta tasks[]")
        return data["tasks"]
    if source == "brick":
        if not isinstance(data, dict) or not isinstance(data.get("tasks"), list):
            raise ValueError("Brick: falta tasks[]")
        return data["tasks"]
    if source == "agentbounties":
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            raise ValueError("AgentBounties: falta items[]")
        return data["items"]
    if source == "taskmarket":
        if not isinstance(data, dict) or not isinstance(data.get("tasks"), list):
            raise ValueError("TaskMarket: falta tasks[]")
        return data["tasks"]
    raise ValueError("Origen desconocido")


def canonical_available(data):
    if not isinstance(data, dict) or data.get("degraded") is True:
        return False
    sources = as_list(data.get("source_statuses"))
    canonical = [s for s in sources
                 if status_alias(as_dict(s).get("source_type")) == "canonical-base"]
    return bool(canonical) and all(s.get("available") is True for s in canonical)


def safe_url(value):
    url = slug_text(value, 350)
    return url if url.startswith("https://") else ""


def normalize(source, item, scanned_at, *, feed_available=True):
    item = as_dict(item)
    id_value = slug_text(item.get("task_id") or item.get("id") or item.get("opportunity_id"), 120)
    category, reasons, proof = classify(source, item, feed_available=feed_available)
    if source == "basedagents":
        url = "https://basedagents.ai/tasks/" + id_value if id_value else ENDPOINTS["basedagents"]
    elif source == "brick":
        url = "https://brick.blue/tasks/" + id_value if id_value else ENDPOINTS["brick"]
    elif source == "taskmarket":
        url = "https://taskmarket.dev/tasks/" + id_value if id_value else ENDPOINTS["taskmarket"]
    else:
        url = safe_url(item.get("authoritative_url") or item.get("url")) or ENDPOINTS["agentbounties"]
    return {
        "source": source, "id": id_value, "title": slug_text(item.get("title") or item.get("name"), 280),
        "category": category, "reasons": reasons, "amount_usdc": proof["amount_usdc"],
        "network": proof["network"], "payment_state": proof["payment_state"],
        "escrow_status": proof["escrow_status"], "deposit_tx_hash": proof["deposit_tx_hash"],
        "state": proof["work_state"], "age_eligibility_verified": False,
        "url": url, "scanned_at": scanned_at,
    }


def scan(mode, fetch=pull_json):
    timestamp = utc_now()
    sources = {}
    data = {}
    files = fixtures() if mode == "offline" else None
    for key, url in ENDPOINTS.items():
        try:
            value = files[key] if files is not None else fetch(url)
            if key != "basedagents_status":
                source_rows(key, value)
            elif not isinstance(value, dict):
                raise ValueError("BasedAgents status sin objeto JSON")
            data[key] = value
            sources[key] = {"ok": True, "url": url, "error": None}
        except (RuntimeError, ValueError, TypeError, KeyError, OSError) as exc:
            sources[key] = {"ok": False, "url": url, "error": slug_text(exc, 230)}

    agent_feed_available = canonical_available(data.get("agentbounties", {}))
    if "agentbounties" in data:
        sources["agentbounties"]["degraded"] = bool(data["agentbounties"].get("degraded"))
        sources["agentbounties"]["canonical_available"] = agent_feed_available
        if not agent_feed_available:
            sources["agentbounties"]["warning"] = "El feed canónico está degradado/no verificable; no se puede confirmar ausencia de tareas."
    rows = []
    for source in ("basedagents", "brick", "agentbounties", "taskmarket"):
        if source not in data:
            continue
        feed_available = agent_feed_available if source == "agentbounties" else True
        for item in source_rows(source, data[source]):
            if isinstance(item, dict):
                rows.append(normalize(source, item, timestamp, feed_available=feed_available))
    counts = {label: sum(row["category"] == label for row in rows) for label in STATUSES}
    return {
        "scanned_at_utc": timestamp,
        "mode": mode,
        "classification_policy": "Conservadora; real-funded = escrow marcado funded + depósito hash Base mainnet reportado por API. Sin verificación independiente on-chain, disponibilidad, legitimidad o elegibilidad legal.",
        "eligible_for_minor_asserted": False,
        "sources": sources, "counts": counts, "rows": rows,
        "notes": [
            "El reporte no solicita, reclama ni cobra recompensas.",
            "Edad, condiciones contractuales, KYC, jurisdicción y retiro permanecen sin verificar.",
            "Un resultado sin real-funded no demuestra que el mercado carezca de oportunidades.",
            "Los campos de financiación reflejan declaraciones de la API pública, no confirmación independiente de blockchain.",
            "No se siguen links ni se envían datos a los mercados.",
        ],
    }


def md_escape(value):
    return (slug_text(value, 300).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace("|", "\\|")
            .replace("[", "\\[").replace("]", "\\]"))


def write_reports(report, output=OUTPUT):
    output.mkdir(parents=True, exist_ok=True)
    json_path = output / "report.json"
    markdown_path = output / "report.md"
    csv_path = output / "scans.csv"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    headers = ["scanned_at", "source", "id", "title", "category", "amount_usdc",
               "network", "payment_state", "escrow_status", "deposit_tx_hash",
               "state", "age_eligibility_verified", "reason", "url"]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=headers)
        writer.writeheader()
        for row in report["rows"]:
            record = dict(row)
            record["scanned_at"] = row["scanned_at"]
            record["reason"] = "; ".join(row["reasons"])
            sanitized = {}
            for key in headers:
                value = str(record.get(key, ""))
                if re.match(r"^\s*[=+\-@]", value):
                    value = "'" + value
                sanitized[key] = value
            writer.writerow(sanitized)

    lines = [
        "# Paid Task Scout — informe público",
        "",
        "**Generado (UTC):** " + report["scanned_at_utc"],
        "**Modo:** " + report["mode"],
        "**Política:** " + report["classification_policy"],
        "",
        "## Fuentes",
        "",
    ]
    for name, source in report["sources"].items():
        state = "OK" if source["ok"] else "ERROR"
        suffix = (" — " + md_escape(source["error"])) if source.get("error") else ""
        if source.get("degraded"):
            suffix += " — DEGRADADO (no se infiere lista vacía)"
        lines.append("- **" + name + "** " + state + suffix + " — " + source["url"])
    lines += ["", "## Conteos por categoría", ""]
    for name in STATUSES:
        lines.append("- **" + name + "**: " + str(report["counts"][name]))
    lines += ["", "## Tareas", "",
              "| Mercado | ID | Título | Estado | USDC declarado | Evidencia / exclusión |",
              "|---|---|---|---|---:|---|"]
    for row in report["rows"]:
        lines.append("| " + md_escape(row["source"]) + " | " +
                     md_escape(row["id"]) + " | " + md_escape(row["title"]) +
                     " | **" + row["category"] + "** | " +
                     md_escape(row["amount_usdc"] or "—") + " | " +
                     md_escape("; ".join(row["reasons"])) + " |")
    lines += ["", "## Limitaciones", ""]
    if report["mode"] == "offline":
        lines.append("- **MODO OFFLINE:** todos los ejemplos son inventados. Una clasificación real-funded en fixture NO acredita ningún depósito real.")
    lines += ["- " + note for note in report["notes"]]
    lines += ["", "**Nota:** ningún registro implica que un menor pueda reclamarlo legalmente."]
    markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, markdown_path, csv_path


def main(argv=None):
    parser = argparse.ArgumentParser(description="Monitor de mercados de tareas para agentes (solo GET, stdlib).")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--offline", action="store_true", help="Leer fixtures sin usar Internet.")
    group.add_argument("--live", action="store_true", help="Realizar GET públicos a las APIs oficiales.")
    parser.add_argument("--output", type=Path, default=OUTPUT, help="Carpeta local para reportes.")
    args = parser.parse_args(argv)
    report = scan("offline" if args.offline else "live")
    paths = write_reports(report, args.output)
    print("Modo:", report["mode"], "|", report["scanned_at_utc"])
    print("Clasificación:", ", ".join(key + "=" + str(n) for key, n in report["counts"].items()))
    for name, info in report["sources"].items():
        print("Fuente:", name, "OK" if info["ok"] else "ERROR", info.get("warning", "") or info.get("error") or "")
    for path in paths:
        print("Generado:", path)
    return 0 if all(info["ok"] for info in report["sources"].values()) else 2


if __name__ == "__main__":
    sys.exit(main())
