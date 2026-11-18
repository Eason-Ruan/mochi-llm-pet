"""Validate four starter metric definitions in D's v0.4 report contract.

This checks report shape, units, arithmetic, and internal accounting only.
It does not read source evidence, execute experiments, or certify performance.
"""

import argparse
import json
import math
from collections import Counter
from datetime import datetime
from hashlib import sha256
from pathlib import Path


VERSION = "0.4"
PROFILES = {
    "delivery.soft_window_first_display_rate": (
        "ratio", "fraction", "occurrence", {"delivery_evidence": "occurrence"}),
    "interruption.user_reported_rate": (
        "ratio", "fraction", "rated_experience", {"experience_records": "experience"}),
    "cost.per_verified_displayed_occurrence": (
        "cost", "currency_per_verified_displayed_occurrence", "verified_displayed_occurrence",
        {"cost_accounting": "call", "delivery_evidence": "occurrence"}),
    "latency.hard_due_p95": (
        "duration", "ms", None, {"latency_measurements": "timing_target"}),
    "semantics.strict_interpretation_accuracy": (
        "ratio", "fraction", "request", {"interpretation_evidence": "request"}),
    "semantics.field_accuracy": (
        "ratio", "fraction", "request", {"interpretation_evidence": "request"}),
    "semantics.clarification_behavior_accuracy": (
        "ratio", "fraction", "request", {"interpretation_evidence": "request"}),
}
GAP_REASONS = {"missing_record", "invalid_record", "missing_linkage", "missing_required_value"}
INVITATION_REASONS = {
    "no_user_interaction_before_deadline", "participant_opted_out_after_enrollment",
    "invitation_capability_unavailable", "invitation_technical_error",
    "protocol_stop_before_invitation", "other",
}
UNAVAILABLE = {"not_measured", "unsupported", "not_applicable", "insufficient_evidence", "failed"}
REPORT_FIELDS = {
    "result_schema_version", "experiment_id", "configuration_id", "dataset_version",
    "cohort_manifest_ref", "evidence_type", "example_only", "as_of", "results",
}
RESULT_FIELDS = {
    "metric_id", "definition_version", "cohort_id", "metric_type", "status", "assessment",
    "value_scope", "value", "unit", "currency", "numerator", "denominator",
    "numerator_unit", "denominator_unit", "statistic", "quantile", "quantile_method",
    "sample_count", "counts", "excluded", "evidence_missing", "not_invited_reason_counts",
    "coverage", "warnings",
}


def need(condition, path, message):
    if not condition:
        raise ValueError(f"{path}: {message}")


def fields(value, required, path, optional=()):
    need(isinstance(value, dict), path, "must be an object")
    need(required <= value.keys(), path, f"missing fields: {sorted(required - value.keys())}")
    need(not value.keys() - required - set(optional), path,
         f"unsupported fields: {sorted(value.keys() - required - set(optional))}")


def text(value, path):
    need(isinstance(value, str) and bool(value.strip()), path, "must be a nonempty string")


def number(value, path, nullable=False):
    if value is None and nullable:
        return
    try:
        valid = type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        valid = False
    need(valid, path, "must be a finite numeric value, not a boolean")


def count(value, path, nullable=False):
    if value is None and nullable:
        return
    need(type(value) is int and value >= 0, path, "must be a nonnegative integer")
    number(value, path)


def close(actual, expected, path):
    need(math.isclose(actual, expected, rel_tol=1e-9, abs_tol=1e-12), path,
         "does not match the declared arithmetic")


def count_map(value, path, nullable_values=False):
    need(isinstance(value, dict), path, "must be a count object")
    for key, item in value.items():
        text(key, path + ".key")
        count(item, path + "." + key, nullable_values)


def check_coverage(value, path):
    fields(value, {"unit", "numerator", "denominator", "value", "status"}, path)
    text(value["unit"], path + ".unit")
    n, d, v, status = (value[k] for k in ("numerator", "denominator", "value", "status"))
    count(n, path + ".numerator", True)
    count(d, path + ".denominator", True)
    if n is None or d is None:
        need(v is None and status == "insufficient_evidence", path, "unknown coverage must remain null")
    else:
        need(n <= d, path, "coverage numerator exceeds denominator")
        if d == 0:
            need(v is None and status == "not_applicable", path, "zero coverage denominator requires null")
        else:
            number(v, path + ".value")
            need(status == "computed", path, "known positive coverage must be computed")
            close(v, n / d, path)


def coverage_matches(result, key, unit, n, d, path):
    need(key in result["coverage"], path, f"missing coverage: {key}")
    value = result["coverage"][key]
    need(value["unit"] == unit and value["numerator"] == n and value["denominator"] == d,
         path + ".coverage." + key, "does not reconcile to metric counts")


def check_gaps(result, domains, codes, path):
    gaps = result["evidence_missing"]
    fields(gaps, set(domains), path + ".evidence_missing")
    for domain, unit in domains.items():
        gap = gaps[domain]
        p = path + ".evidence_missing." + domain
        fields(gap, {"unit", "count", "reason_counts"}, p)
        need(gap["unit"] == unit, p + ".unit", "incorrect evidence unit")
        count(gap["count"], p + ".count", True)
        reasons = gap["reason_counts"]
        if reasons is not None:
            count_map(reasons, p + ".reason_counts")
            need(set(reasons) <= GAP_REASONS, p, "unknown evidence-gap reason")
        if gap["count"] is None:
            if result["status"] not in {"not_measured", "unsupported"}:
                need("EVIDENCE_INVENTORY_UNKNOWN" in codes, p, "unknown inventory requires a warning")
        else:
            need(reasons is not None and sum(reasons.values()) == gap["count"], p,
                 "reason counts do not sum to the missing-evidence count")
            if gap["count"] == 0:
                need(reasons == {}, p, "zero evidence gaps require an empty reason map")


def require_counts(result, names, path):
    need(set(names) <= result["counts"].keys(), path + ".counts", "missing metric-specific counts")


def full_evidence(result):
    return (all(g["count"] == 0 for g in result["evidence_missing"].values())
            and all(v["status"] == "computed" and v["numerator"] == v["denominator"]
                    for v in result["coverage"].values()))


def check_metric(result, path):
    fields(result, RESULT_FIELDS, path, {"bounds"})
    for key in ("metric_id", "definition_version", "cohort_id"):
        text(result[key], path + "." + key)
    metric = result["metric_id"]
    need(metric in PROFILES, path + ".metric_id", "unsupported metric; this profile supports the declared metric IDs")
    kind, unit, denominator_unit, domains = PROFILES[metric]
    need(result["definition_version"] == VERSION, path, "unsupported definition version")
    need(result["metric_type"] == kind and result["unit"] == unit, path, "incorrect metric type or unit")
    status, value = result["status"], result["value"]
    text(status, path + ".status")
    text(result["assessment"], path + ".assessment")
    need(status in {"computed", "preliminary"} | UNAVAILABLE, path, "unknown status")
    need(result["assessment"] in {"passed", "failed", "not_evaluated"}, path, "unknown assessment")
    warnings = result["warnings"]
    need(isinstance(warnings, list), path + ".warnings", "must be a list")
    codes = set()
    for warning in warnings:
        fields(warning, {"code", "message"}, path + ".warnings")
        for key in ("code", "message"):
            text(warning[key], path + ".warnings." + key)
        codes.add(warning["code"])
    if status != "computed":
        need(bool(warnings), path, "non-computed results require a warning")
        need(result["assessment"] == "not_evaluated", path, "partial/unavailable results cannot pass acceptance")
    if status in UNAVAILABLE:
        need(value is None and result["value_scope"] == "not_available", path, "unavailable point values must be null")
    else:
        number(value, path + ".value")
        need(value >= 0, path, "negative value is invalid for this metric")
        if kind == "ratio":
            need(value <= 1, path, "ratio must be in 0..1")
        expected_scope = "eligible_cohort" if status == "computed" else (
            "observed_respondents" if metric == "interruption.user_reported_rate" else "measured_subset")
        need(result["value_scope"] == expected_scope, path, "incorrect value_scope")
    if status == "preliminary":
        need(metric in {"interruption.user_reported_rate", "latency.hard_due_p95"}, path,
             "this metric does not allow a preliminary point estimate")

    count_map(result["counts"], path + ".counts", True)
    count_map(result["excluded"], path + ".excluded", True)
    if any(v is None for v in (*result["counts"].values(), *result["excluded"].values())):
        need(bool(warnings), path, "unknown counts require a warning")
    need(isinstance(result["coverage"], dict) and bool(result["coverage"]), path, "coverage is required")
    for name, coverage in result["coverage"].items():
        text(name, path + ".coverage.key")
        check_coverage(coverage, path + ".coverage." + name)
        if coverage["status"] == "insufficient_evidence":
            need(bool(warnings), path, "unknown coverage requires a warning")
    check_gaps(result, domains, codes, path)
    if status == "not_measured":
        need(all(g["count"] is None and g["reason_counts"] is None
                 for g in result["evidence_missing"].values()), path,
             "unmeasured evidence must remain unknown, not a verified zero")

    if kind == "cost":
        currency = result["currency"]
        need(isinstance(currency, str) and len(currency) == 3 and currency.isascii()
             and currency.isalpha() and currency.isupper(), path, "currency must be an uppercase three-letter code")
    else:
        need(result["currency"] is None, path, "nonmonetary results must have null currency")
    if kind == "duration":
        for field in ("numerator", "denominator", "numerator_unit", "denominator_unit"):
            need(result[field] is None, path + "." + field, "percentiles do not use division components")
        need(result["statistic"] == "percentile" and type(result["quantile"]) in (int, float)
             and result["quantile"] == 0.95 and result["quantile_method"] == "nearest_rank", path,
             "this metric requires P95 with nearest_rank")
        count(result["sample_count"], path + ".sample_count", True)
        if value is not None:
            need(result["sample_count"] is not None and result["sample_count"] > 0, path,
                 "a percentile needs measured samples")
    else:
        need(result["statistic"] == "scalar" and all(result[k] is None for k in (
            "quantile", "quantile_method", "sample_count")), path, "scalar statistical fields must be null")
        number(result["numerator"], path + ".numerator", True)
        count(result["denominator"], path + ".denominator", True)
        if kind == "ratio":
            count(result["numerator"], path + ".numerator", True)
        need(result["numerator_unit"] == ("currency" if kind == "cost" else denominator_unit)
             and result["denominator_unit"] == denominator_unit, path, "incorrect component units")
        n, d = result["numerator"], result["denominator"]
        if n is not None:
            need(n >= 0, path, "numerator must be nonnegative")
        if n is not None and d is not None and kind == "ratio":
            need(n <= d, path, "ratio numerator exceeds denominator")
        if value is not None:
            need(n is not None and d is not None and d > 0, path, "numeric unit cost/rate needs a positive denominator")
            close(value, n / d, path + ".value")
        if status == "not_applicable":
            need(d == 0, path, "this metric is not applicable only with an established zero denominator")

    if "bounds" in result:
        bounds = result["bounds"]
        fields(bounds, {"lower", "upper", "unit", "value_scope"}, path + ".bounds")
        need(metric == "delivery.soft_window_first_display_rate" and value is None
             and status == "insufficient_evidence", path,
             "this profile supports bounds only for an unavailable window-success point estimate")
        for key in ("lower", "upper"):
            number(bounds[key], path + ".bounds." + key)
        need(0 <= bounds["lower"] <= bounds["upper"] <= 1 and bounds["unit"] == "fraction"
             and bounds["value_scope"] == "eligible_cohort", path, "invalid success-rate bounds")

    if metric == "delivery.soft_window_first_display_rate":
        require_counts(result, {"total_occurrences", "eligible_occurrences"}, path)
        c = result["counts"]
        eligible, total = c["eligible_occurrences"], c["total_occurrences"]
        need(result["denominator"] == eligible, path, "window denominator is not eligible occurrences")
        if eligible is not None and total is not None and all(v is not None for v in result["excluded"].values()):
            need(total == eligible + sum(result["excluded"].values()), path, "occurrence exclusions do not reconcile")
        missing = result["evidence_missing"]["delivery_evidence"]["count"]
        if eligible is not None and missing is not None:
            need(missing <= eligible, path, "evidence gaps exceed eligible occurrences")
        usable = eligible - missing if eligible is not None and missing is not None else None
        coverage_matches(result, "usable_occurrence_evidence", "occurrence", usable, eligible, path)
        if status == "computed":
            need(full_evidence(result), path, "window point estimate requires complete evidence")
            need(total is not None and all(v is not None for v in result["excluded"].values()),
                 path, "computed window rate requires a reconciled occurrence population")
        if "bounds" in result:
            successes = result["numerator"]
            need(eligible is not None and eligible > 0 and missing is not None
                 and successes is not None and successes <= eligible - missing, path,
                 "bounds require known successes, eligible population, and undetermined occurrences")
            close(result["bounds"]["lower"], successes / eligible, path + ".bounds.lower")
            close(result["bounds"]["upper"], (successes + missing) / eligible, path + ".bounds.upper")

    elif metric == "interruption.user_reported_rate":
        names = {"eligible_experiences", "invited", "rated", "declined", "not_seen", "timed_out",
                 "response_pending", "technical_error", "not_invited", "invitation_pending"}
        require_counts(result, names, path)
        c = result["counts"]
        missing = result["evidence_missing"]["experience_records"]["count"]
        if missing is not None and c["eligible_experiences"] is not None:
            need(missing <= c["eligible_experiences"], path,
                 "experience evidence gaps exceed eligible experiences")
        for total_name, parts in (
            ("invited", ("rated", "declined", "not_seen", "timed_out", "response_pending", "technical_error")),
            ("eligible_experiences", ("invited", "not_invited", "invitation_pending")),
        ):
            if c[total_name] is not None and all(c[k] is not None for k in parts):
                need(c[total_name] == sum(c[k] for k in parts), path, "experience statuses do not reconcile")
        need(result["denominator"] == c["rated"], path, "interruption denominator is rated experiences only")
        for name, n, d in (("invitation", c["invited"], c["eligible_experiences"]),
                           ("rated_response", c["rated"], c["invited"]),
                           ("overall_rated", c["rated"], c["eligible_experiences"])):
            coverage_matches(result, name, "experience", n, d, path)
        distribution = result["not_invited_reason_counts"]
        if distribution is None:
            need(c["not_invited"] != 0 and "INVITATION_REASON_EVIDENCE_GAP" in codes, path,
                 "unknown invitation reasons need an explicit evidence warning")
            need(result["evidence_missing"]["experience_records"]["count"] != 0, path,
                 "unknown reason distribution contradicts complete experience evidence")
        else:
            count_map(distribution, path + ".not_invited_reason_counts")
            need(set(distribution) <= INVITATION_REASONS, path, "unknown non-invitation reason")
            need(c["not_invited"] is not None and sum(distribution.values()) == c["not_invited"], path,
                 "non-invitation reasons do not reconcile")
            if c["not_invited"] == 0:
                need(distribution == {}, path, "zero non-invitations require an empty reason map")
        if value is not None:
            need(c["eligible_experiences"] is not None and c["rated"] is not None, path,
                 "respondent estimate requires a known population")
            if c["rated"] < c["eligible_experiences"]:
                need(status == "preliminary" and "NONRESPONSE_COVERAGE" in codes, path,
                     "partial response must be preliminary with a coverage warning")
            else:
                need(status == "computed" and full_evidence(result), path,
                     "full response estimate requires complete evidence")

    elif metric == "cost.per_verified_displayed_occurrence":
        require_counts(result, {"input_requests", "verified_displayed_occurrences", "late_displays", "user_completed_tasks"}, path)
        c = result["counts"]
        need(result["denominator"] == c["verified_displayed_occurrences"], path,
             "cost denominator is verified displayed occurrences, not completed tasks")
        need({"accounted_calls", "occurrence_outcomes"} <= result["coverage"].keys(), path,
             "cost accounting and occurrence outcome coverage are required")
        for name, unit, domain in (("accounted_calls", "call", "cost_accounting"),
                                   ("occurrence_outcomes", "occurrence", "delivery_evidence")):
            cov = result["coverage"][name]
            need(cov["unit"] == unit, path, "incorrect cost coverage unit")
            gap = result["evidence_missing"][domain]["count"]
            if gap is not None and cov["denominator"] is not None and cov["numerator"] is not None:
                need(gap == cov["denominator"] - cov["numerator"], path,
                     "cost evidence gaps do not reconcile with accounting coverage")
        if c["late_displays"] is not None and c["verified_displayed_occurrences"] is not None:
            need(c["late_displays"] <= c["verified_displayed_occurrences"], path, "late displays exceed displays")
        outcomes = result["coverage"]["occurrence_outcomes"]["numerator"]
        if outcomes is not None and c["verified_displayed_occurrences"] is not None:
            need(c["verified_displayed_occurrences"] <= outcomes, path,
                 "verified displays exceed occurrences with established outcomes")
        if status == "computed":
            need(full_evidence(result), path, "cost point estimate requires complete accounting and outcome evidence")

    elif metric in {"semantics.strict_interpretation_accuracy",
                    "semantics.field_accuracy", "semantics.clarification_behavior_accuracy"}:
        require_counts(result, {"eligible_requests", "assessed_requests", "correct_requests"}, path)
        c = result["counts"]
        if c["eligible_requests"] is not None and c["assessed_requests"] is not None:
            need(c["assessed_requests"] <= c["eligible_requests"], path,
                 "assessed requests exceed eligible requests")
        if c["correct_requests"] is not None and c["assessed_requests"] is not None:
            need(c["correct_requests"] <= c["assessed_requests"], path,
                 "correct requests exceed assessed requests")
        need(result["denominator"] == c["assessed_requests"], path,
             "semantics denominator is assessed requests")
        coverage_matches(result, "prediction_records", "request", c["assessed_requests"],
                         c["eligible_requests"], path)
        if status == "computed":
            need(full_evidence(result), path, "semantics point estimate requires complete prediction evidence")

    else:
        require_counts(result, {"eligible_timing_targets", "measured", "missing_latency", "timed_out", "early_render_violations"}, path)
        c = result["counts"]
        need(result["sample_count"] == c["measured"], path, "percentile sample count does not match measured count")
        coverage_matches(result, "latency_measurements", "timing_target", c["measured"], c["eligible_timing_targets"], path)
        missing = result["evidence_missing"]["latency_measurements"]["count"]
        need(c["missing_latency"] == missing, path + ".counts.missing_latency",
             "must match the latency evidence-gap count, including an unknown inventory")
        eligible = c["eligible_timing_targets"]
        if eligible is not None:
            for key in ("missing_latency", "timed_out", "early_render_violations"):
                if c[key] is not None:
                    need(c[key] <= eligible, path + ".counts." + key, "count exceeds eligible timing targets")
            if missing is not None and c["measured"] is not None:
                need(missing <= eligible - c["measured"], path,
                     "missing timing evidence overlaps declared valid measurements")
        if status == "not_applicable":
            need(eligible == 0 and c["measured"] == 0, path,
                 "not-applicable percentile requires an established empty eligible population and no measured samples")
        if value is not None:
            need(c["eligible_timing_targets"] is not None, path, "latency population must be known")
            if c["measured"] < c["eligible_timing_targets"]:
                need(status == "preliminary" and "LATENCY_COVERAGE_GAP" in codes, path,
                     "partial latency must be preliminary with a coverage warning")
            else:
                need(status == "computed" and full_evidence(result), path,
                     "full latency estimate requires complete evidence")

    if metric != "interruption.user_reported_rate":
        need(result["not_invited_reason_counts"] is None, path, "invitation reasons do not apply to this metric")
    return (metric, result["cohort_id"], result["definition_version"])


def validate_report(report):
    """Validate the supported contract profile, not the truth of source evidence."""
    fields(report, REPORT_FIELDS, "report")
    need(report["result_schema_version"] == VERSION, "report", "unsupported result schema version")
    for key in ("experiment_id", "configuration_id"):
        text(report[key], "report." + key)
    need(type(report["example_only"]) is bool, "report.example_only", "must be a boolean")
    text(report["evidence_type"], "report.evidence_type")
    need(report["evidence_type"] in {"synthetic_example", "synthetic_replay", "native_scenario", "live"},
         "report.evidence_type", "unknown evidence type")
    need(report["example_only"] == (report["evidence_type"] == "synthetic_example"), "report",
         "format examples require synthetic_example and example_only=true together")
    for key in ("dataset_version", "cohort_manifest_ref"):
        if report[key] is not None or not report["example_only"]:
            text(report[key], "report." + key)
    text(report["as_of"], "report.as_of")
    as_of = datetime.fromisoformat(report["as_of"].replace("Z", "+00:00"))
    need(as_of.utcoffset() is not None, "report.as_of", "must include a UTC offset")
    results = report["results"]
    need(isinstance(results, list) and bool(results), "report.results", "must be a nonempty list")
    seen = set()
    for i, result in enumerate(results):
        key = check_metric(result, f"results[{i}]")
        if report["example_only"]:
            need(result["assessment"] == "not_evaluated", f"results[{i}]",
                 "format examples cannot establish acceptance")
        if report["evidence_type"] == "synthetic_replay" and key[0] != "delivery.soft_window_first_display_rate":
            need(result["status"] in UNAVAILABLE, f"results[{i}]",
                 "synthetic replay cannot measure native cost, real latency, or user experience")
        need(key not in seen, f"results[{i}]", "duplicate metric/cohort/version")
        seen.add(key)
    return {
        "status": "passed", "validation_type": "metric_report_contract",
        "validator_profile": "four_starter_metrics", "result_schema_version": VERSION,
        "metric_count": len(results), "metric_status_counts": dict(Counter(r["status"] for r in results)),
        "example_only": report["example_only"], "source_evidence_verification": "not_performed",
        "raw_percentile_recomputation": "not_performed", "models_called_by_validator": False,
    }


def reject_constant(value):
    raise ValueError(f"Non-finite JSON constant: {value}")


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def load_report(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"),
                      parse_constant=reject_constant, object_pairs_hook=unique_object)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    args = parser.parse_args()
    try:
        raw = args.report.read_bytes()
        report = json.loads(raw.decode("utf-8-sig"), parse_constant=reject_constant, object_pairs_hook=unique_object)
        result = validate_report(report)
        result["checked_report_sha256"] = sha256(raw).hexdigest()
    except (OSError, ValueError, TypeError, KeyError, OverflowError) as error:
        print(json.dumps({"status": "failed", "validation_type": "metric_report_contract", "error": str(error)}, indent=2))
        raise SystemExit(1)
    print(json.dumps(result, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
