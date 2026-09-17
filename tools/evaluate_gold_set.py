"""Evaluate saved review JSON against lawyer-approved labels without calling a model."""
import argparse
import json
from pathlib import Path


def _load_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _validated_label_file(path, allow_draft):
    data = _load_json(path)
    required = {"schema_version", "contract_id", "source_file_sha256", "input_text_sha256",
                "status", "labels",
                "exhaustively_reviewed"}
    missing = sorted(required - set(data))
    if missing:
        raise ValueError(f"{path}: missing fields {missing}")
    if data["status"] == "approved":
        if not data.get("approved_by") or not data.get("approved_at"):
            raise ValueError(f"{path}: approved label set lacks approver metadata")
    elif not allow_draft:
        return None
    for label in data["labels"]:
        if not {"label_id", "disposition", "clause_ids", "risk_type_tag", "fact"} <= set(label):
            raise ValueError(f"{path}: incomplete label {label.get('label_id')}")
        if label["disposition"] not in ("risk", "acceptable"):
            raise ValueError(f"{path}: invalid disposition")
    return data


def _prediction_hash(result):
    return (result.get("provenance") or {}).get("input_text_sha256")


def _risks(result):
    return [risk for key in ("rule_risks", "legal_risks", "commercial_risks")
            for risk in result.get(key, [])]


def _match(label, risk):
    if label["risk_type_tag"] != risk.get("risk_type_tag", "undefined"):
        return False
    expected = set(label.get("clause_ids") or [])
    actual = {risk.get("clause_id"), *(risk.get("related_clause_ids") or [])} - {None}
    return not expected or bool(expected & actual)


def evaluate(label, result):
    if label["input_text_sha256"] != _prediction_hash(result):
        raise ValueError(f"{label['contract_id']}: prediction source hash differs from labels")
    predictions = _risks(result)
    risk_labels = [item for item in label["labels"] if item["disposition"] == "risk"]
    acceptable = [item for item in label["labels"] if item["disposition"] == "acceptable"]
    matched_labels = [item["label_id"] for item in risk_labels if any(_match(item, p) for p in predictions)]
    contradicted = [item["label_id"] for item in acceptable if any(_match(item, p) for p in predictions)]
    mapped_predictions = {index for index, prediction in enumerate(predictions)
                          if any(_match(item, prediction) for item in label["labels"])}
    exhaustive = label["status"] == "approved" and label["exhaustively_reviewed"]
    return {
        "contract_id": label["contract_id"],
        "label_status": label["status"],
        "official_metrics": exhaustive,
        "labeled_risks": len(risk_labels),
        "matched_labeled_risks": len(matched_labels),
        "labeled_recall": round(len(matched_labels) / len(risk_labels), 4) if risk_labels else None,
        "acceptable_labels_triggered": contradicted,
        "prediction_count": len(predictions),
        "unmapped_predictions": len(predictions) - len(mapped_predictions),
        "precision": (round(len(mapped_predictions) / len(predictions), 4)
                      if exhaustive and predictions else None),
        "matched_label_ids": matched_labels,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--labels", type=Path, required=True)
    parser.add_argument("--predictions", type=Path, nargs="+", required=True)
    parser.add_argument("--include-draft", action="store_true")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    labels = {}
    for path in args.labels.glob("*.json"):
        if path.name == "label_schema.json":
            continue
        item = _validated_label_file(path, args.include_draft)
        if item:
            labels[item["input_text_sha256"]] = item
    rows, unmatched = [], []
    for path in args.predictions:
        result = _load_json(path)
        label = labels.get(_prediction_hash(result))
        if label:
            rows.append(evaluate(label, result))
        else:
            unmatched.append(str(path))
    official = [row for row in rows if row["official_metrics"]]
    output = {"official_contracts":len(official), "evaluated_contracts":len(rows),
              "unmatched_predictions":unmatched, "results":rows,
              "notice":("没有评审批准且穷尽审阅的标签，当前结果不能作为正式准确率。"
                        if not official else "正式指标只汇总 official_metrics=true 的合同。")}
    text = json.dumps(output, ensure_ascii=False, indent=2)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
