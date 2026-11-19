"""
evaluate.py - the accuracy and cost report.   (Chunk 11)

This is how we answer the question that decides the startup: does Kannada
voice become the right ledger entries, reliably enough?

It runs a TEST SET (eval/kannada_cases.json: sentences with the right answer
written next to each) through the real engines and counts how many come out
right. Two ways to run it:

    --stage text    skip speech: feed the sentences straight to the AI.
                    Measures ONLY the "understand the words" step. Needs no
                    recordings, so you can run it today.
    --stage voice   run your RECORDINGS through speech-to-text, then the AI.
                    Measures the whole chain, the real test. Recordings go in
                    eval/audio/ named after the case:   k01.ogg   or, for
                    several speakers,   k01__anita.m4a   k01__raju.m4a

Every case ends in exactly ONE of four outcomes:

    correct        understood right and NO question needed: it would be saved
                   silently, correctly. This is the number the 90% bar is about.
    asks           the system is not sure and would ask the owner. Friction,
                   but no harm: nothing wrong is saved.
    silently_wrong understood WRONG and not flagged: a wrong entry would be saved
                   without anyone noticing. THE DANGEROUS ONE. Should be about 0.
    failed         an engine crashed or heard nothing; the owner is told to try again.

PASS needs  correct >= 90%  AND  silently_wrong <= 2%  (change with the flags).

Also reported: how often the intent was right, customer / amount / kind accuracy
per field, spelling near-misses of customer names (the duplicate-customer risk),
speech accuracy (character error rate), speed, and cost if you pass prices.

Examples:
    python -m app.evaluate --stage text --engine hosted
    python -m app.evaluate --stage voice --stt local --engine local
    python -m app.evaluate --stage voice --stt hosted --engine hosted --cost-stt 0.5 --cost-llm 0.2

Each run also saves a JSON report in eval/reports/ so you can compare runs.
"""
from __future__ import annotations

import argparse
import json
import unicodedata
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path

from app import extraction, ledger, matching, speech
from app.extraction import Extraction, ExtractionError, parse_due
from app.speech import SpeechError

DEFAULT_CASES = "eval/kannada_cases.json"
DEFAULT_AUDIO_DIR = "eval/audio"
DEFAULT_REPORT_DIR = "eval/reports"
DEFAULT_TODAY = date(2026, 11, 20)      # a Friday; fixed so reports can be compared

PASS_CORRECT = 0.90                     # at least this share must be correct
PASS_MAX_SILENTLY_WRONG = 0.02          # at most this share may be silently wrong

OUTCOMES = ("correct", "asks", "silently_wrong", "failed")


# ---------------------------------------------------------------------------
# The test set
# ---------------------------------------------------------------------------

def load_cases(path: str) -> tuple[list[dict], date | None]:
    """
    Read the test set. Returns (cases, the fixed 'today' of the file, if given).
    Raises ValueError with a clear message if a case is malformed.

    Each case: {"id": "k01", "tags": [...], "say": "the sentence",
                "expect": {...the right answer...}}
    See eval/kannada_cases.json for every kind of expectation.
    """
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    cases = data["cases"] if isinstance(data, dict) else data
    seen: set[str] = set()
    for case in cases:
        for key in ("id", "say", "expect"):
            if key not in case:
                raise ValueError(f"A case is missing {key!r}: {case}")
        if case["id"] in seen:
            raise ValueError(f"Duplicate case id {case['id']!r}")
        seen.add(case["id"])
        expect = case["expect"]
        if expect.get("intent") not in extraction.INTENTS:
            raise ValueError(f"Case {case['id']}: expect.intent must be one of {extraction.INTENTS}")
        if expect["intent"] == "entries" and not expect.get("must_flag") and not expect.get("entries"):
            raise ValueError(f"Case {case['id']}: an 'entries' case needs entries (or must_flag)")
        if expect["intent"] in ("balance", "different_person") and not expect.get("customer"):
            raise ValueError(f"Case {case['id']}: {expect['intent']} needs a customer")
    today = date.fromisoformat(data["today"]) if isinstance(data, dict) and data.get("today") else None
    return cases, today


def find_recordings(audio_dir: str, case_ids: list[str]) -> tuple[dict[str, list[tuple[str | None, str]]], list[str]]:
    """
    Find recordings for the cases. A file named  k01.ogg  is case k01;
    k01__anita.m4a  is case k01 spoken by "anita".
    Returns ({case id: [(speaker or None, path), ...]}, files that match no case).
    """
    found: dict[str, list[tuple[str | None, str]]] = defaultdict(list)
    unmatched: list[str] = []
    folder = Path(audio_dir)
    if not folder.is_dir():
        return {}, []
    for path in sorted(folder.iterdir()):
        if path.suffix.lower() not in speech.AUDIO_MIME_TYPES:
            continue
        case_id, _, speaker = path.stem.partition("__")
        if case_id in case_ids:
            found[case_id].append((speaker or None, str(path)))
        else:
            unmatched.append(path.name)
    return dict(found), unmatched


# ---------------------------------------------------------------------------
# Measuring
# ---------------------------------------------------------------------------

def _speech_key(text: str) -> str:
    """Text reduced to its letters for comparing speech: no spaces, punctuation or case."""
    text = unicodedata.normalize("NFC", text).lower()
    return "".join(ch for ch in text if unicodedata.category(ch)[0] in "LMN")


def character_error_rate(reference: str, heard: str) -> float:
    """
    How wrong the speech-to-text was, 0 = perfect, 1 = completely wrong.
    = (letters to insert, delete or change to turn `heard` into `reference`) / length of reference.
    Spaces, punctuation and capital letters are ignored (speech engines differ in those).
    """
    ref, hyp = _speech_key(reference), _speech_key(heard)
    if not ref:
        return 0.0 if not hyp else 1.0
    previous = list(range(len(hyp) + 1))
    for i, ref_char in enumerate(ref, start=1):
        current = [i]
        for j, hyp_char in enumerate(hyp, start=1):
            current.append(min(previous[j] + 1,                       # delete
                               current[j - 1] + 1,                    # insert
                               previous[j - 1] + (ref_char != hyp_char)))  # change
        previous = current
    return min(1.0, previous[-1] / len(ref))


def names_match(expected, got: str | None) -> bool:
    """Is `got` the expected customer? `expected` is a name or a list of acceptable spellings."""
    if got is None:
        return False
    options = expected if isinstance(expected, list) else [expected]
    return ledger.clean_name(got) in {ledger.clean_name(option) for option in options}


def _name_near_miss(expected, got: str | None) -> bool:
    """Wrong spelling that could be the same person (this is how duplicate customers are born)."""
    if got is None or names_match(expected, got):
        return False
    options = expected if isinstance(expected, list) else [expected]
    return any(matching.could_be_same_person(ledger.clean_name(o), ledger.clean_name(got)) for o in options)


def _entry_fields_match(expected: dict, got, today: date) -> dict[str, bool]:
    """Which fields of one understood entry equal the expected entry."""
    fields = {
        "kind": got.kind == expected["kind"],
        "customer": (got.customer is None) if expected.get("customer") is None
                    else names_match(expected["customer"], got.customer),
        "amount": got.amount == expected["amount"],
    }
    if "due" in expected:       # only compared when the case says what the due date must be
        fields["due"] = got.due_date == (parse_due(expected["due"], today)[0] if expected["due"] else None)
    return fields


def _pair_entries(expected_entries: list[dict], got_entries, today: date):
    """
    Pair each expected entry with the understood entry that matches it best
    (order does not matter). Returns (pairs [(expected, got or None, fields)], leftover got entries).
    """
    unused = list(got_entries)
    pairs = []
    for expected in expected_entries:
        best_index, best_fields, best_score = None, None, -1
        for index, got in enumerate(unused):
            fields = _entry_fields_match(expected, got, today)
            score = sum(fields.values())
            if score > best_score:
                best_index, best_fields, best_score = index, fields, score
        if best_index is None:
            pairs.append((expected, None, {}))
        else:
            pairs.append((expected, unused.pop(best_index), best_fields))
    return pairs, unused


def _describe_expected(expect: dict) -> str:
    if expect.get("must_flag"):
        return "should ask the owner (kind unclear)"
    if expect["intent"] == "entries":
        return "; ".join(
            f"{e['kind']} {(e['customer'][0] if isinstance(e.get('customer'), list) else e.get('customer')) or '(cash)'} "
            f"₹{e['amount']}" + (f" due {e['due']}" if e.get("due") else "")
            for e in expect["entries"])
    customer = expect.get("customer")
    customer = customer[0] if isinstance(customer, list) else customer
    return expect["intent"] + (f" {customer}" if customer else "")


def _describe_understood(result: Extraction) -> str:
    parts = []
    if result.intent == "entries":
        parts += [f"{e.kind} {e.customer or '(cash)'} ₹{e.amount}" + (f" due {e.due_date}" if e.due_date else "")
                  for e in result.entries]
        parts += [f"REJECTED({r.reason})" for r in result.rejected]
        return "; ".join(parts) or "entries (none usable)"
    return result.intent + (f" {result.customer}" if result.customer else "")


@dataclass
class CaseResult:
    """The outcome of ONE case (one sentence, or one recording of it)."""
    case_id: str
    speaker: str | None
    tags: list[str]
    outcome: str                            # one of OUTCOMES
    said: str                               # the sentence the speaker was meant to say
    transcript: str | None = None           # what speech-to-text heard (voice stage)
    cer: float | None = None                # speech error (voice stage)
    expected: str = ""
    understood: str = ""
    intent_ok: bool = False
    entry_fields: dict = field(default_factory=dict)    # per field: [right, total]
    name_near_misses: int = 0
    stt_seconds: float | None = None
    llm_seconds: float | None = None
    error: str | None = None
    raw: str | None = None                  # the AI's raw reply when it was unusable


def score_case(case: dict, result: Extraction, today: date) -> dict:
    """
    Compare what we understood with the right answer. Returns
    {"outcome", "intent_ok", "entry_fields", "name_near_misses"}.
    See the top of this file for the four outcomes.
    """
    expect = case["expect"]
    intent_ok = result.intent == expect["intent"]
    entry_fields: dict[str, list[int]] = {}
    near_misses = 0
    flagged = result.needs_attention

    # Cases where the RIGHT behaviour is to ask or to say "unclear"
    if expect.get("must_flag") or expect["intent"] == "unclear":
        content_ok = flagged if expect.get("must_flag") else result.intent == "unclear"
        return {"outcome": "correct" if content_ok else "silently_wrong", "intent_ok": intent_ok,
                "entry_fields": {}, "name_near_misses": 0}

    content_ok = intent_ok
    if expect["intent"] == "entries" and intent_ok:
        pairs, extras = _pair_entries(expect["entries"], result.entries, today)
        for expected, got, fields in pairs:
            for name, right in fields.items():
                counts = entry_fields.setdefault(name, [0, 0])
                counts[0] += int(right)
                counts[1] += 1
            if got is None:
                for name in ("kind", "customer", "amount"):
                    entry_fields.setdefault(name, [0, 0])[1] += 1
            elif expected.get("customer") and not fields.get("customer"):
                near_misses += _name_near_miss(expected["customer"], got.customer)
        content_ok = (all(all(f.values()) and g is not None for _, g, f in pairs)
                      and not extras and not result.rejected)
    elif expect["intent"] in ("balance", "different_person") and intent_ok:
        right = names_match(expect["customer"], result.customer)
        entry_fields["customer"] = [int(right), 1]
        content_ok = right
        near_misses += _name_near_miss(expect["customer"], result.customer)

    if content_ok and not flagged:
        outcome = "correct"
    elif flagged:
        outcome = "asks"
    else:
        outcome = "silently_wrong"
    return {"outcome": outcome, "intent_ok": intent_ok, "entry_fields": entry_fields,
            "name_near_misses": near_misses}


# ---------------------------------------------------------------------------
# Running a whole test set
# ---------------------------------------------------------------------------

def run_evaluation(cases: list[dict], *, stage: str, transcribe_fn, extract_fn, today: date,
                   recordings: dict[str, list[tuple[str | None, str]]] | None = None) -> list[CaseResult]:
    """
    Run every case and return one CaseResult per sentence (text stage) or per
    recording (voice stage).

    transcribe_fn(path) -> speech.Transcript        (voice stage only)
    extract_fn(text, today) -> extraction.Extraction
    They are passed in so tests can use stand-ins; the command line passes the real ones.
    """
    if stage not in ("text", "voice"):
        raise ValueError("stage must be 'text' or 'voice'")
    results: list[CaseResult] = []

    for case in cases:
        if stage == "text":
            jobs = [(None, None)]
        else:
            jobs = [(speaker, path) for speaker, path in (recordings or {}).get(case["id"], [])]

        for speaker, audio_path in jobs:
            row = CaseResult(case_id=case["id"], speaker=speaker, tags=case.get("tags", []),
                             outcome="failed", said=case["say"], expected=_describe_expected(case["expect"]))
            text = case["say"]

            if stage == "voice":
                try:
                    heard = transcribe_fn(audio_path)
                except SpeechError as err:
                    row.error = f"speech: {err}"
                    results.append(row)
                    continue
                row.transcript, row.stt_seconds = heard.text, heard.elapsed_seconds
                row.cer = round(character_error_rate(case["say"], heard.text), 3)
                if heard.is_empty:
                    row.error = "speech: nothing heard"
                    results.append(row)
                    continue
                text = heard.text

            try:
                understood = extract_fn(text, today)
            except ExtractionError as err:
                row.error, row.raw = f"ai: {err}", err.raw
                results.append(row)
                continue

            scored = score_case(case, understood, today)
            row.outcome, row.intent_ok = scored["outcome"], scored["intent_ok"]
            row.entry_fields, row.name_near_misses = scored["entry_fields"], scored["name_near_misses"]
            row.understood, row.llm_seconds, row.raw = _describe_understood(understood), understood.elapsed_seconds, understood.raw_text
            results.append(row)
    return results


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------

def _share(count: int, total: int) -> float:
    return round(count / total, 4) if total else 0.0


def summarize(results: list[CaseResult], *, pass_correct: float = PASS_CORRECT,
              max_silently_wrong: float = PASS_MAX_SILENTLY_WRONG,
              cost_stt: float = 0.0, cost_llm: float = 0.0) -> dict:
    """Turn the per-case results into the numbers of the report (a plain dictionary, easy to save)."""
    total = len(results)
    counts = Counter(r.outcome for r in results)
    rates = {name: _share(counts[name], total) for name in OUTCOMES}

    field_totals: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for r in results:
        for name, (right, count) in r.entry_fields.items():
            field_totals[name][0] += right
            field_totals[name][1] += count

    def group(key_of):
        buckets = defaultdict(list)
        for r in results:
            for key in key_of(r):
                buckets[key].append(r)
        return {key: {"n": len(rows), "correct": _share(sum(r.outcome == "correct" for r in rows), len(rows)),
                      "silently_wrong": _share(sum(r.outcome == "silently_wrong" for r in rows), len(rows)),
                      **({"avg_cer": round(sum(r.cer for r in rows) / len(rows), 3)}
                         if all(r.cer is not None for r in rows) else {})}
                for key, rows in sorted(buckets.items(), key=lambda item: str(item[0]))}

    cers = [r.cer for r in results if r.cer is not None]
    stt_times = [r.stt_seconds for r in results if r.stt_seconds is not None]
    llm_times = [r.llm_seconds for r in results if r.llm_seconds is not None]
    scored = [r for r in results if r.outcome != "failed"]
    return {
        "total": total,
        "counts": {name: counts[name] for name in OUTCOMES},
        "rates": rates,
        "intent_accuracy": _share(sum(r.intent_ok for r in scored), len(scored)),
        "field_accuracy": {name: _share(right, count) for name, (right, count) in sorted(field_totals.items())},
        "name_near_misses": sum(r.name_near_misses for r in results),
        "avg_cer": round(sum(cers) / len(cers), 3) if cers else None,
        "avg_stt_seconds": round(sum(stt_times) / len(stt_times), 2) if stt_times else None,
        "avg_llm_seconds": round(sum(llm_times) / len(llm_times), 2) if llm_times else None,
        "total_cost": round(len(stt_times) * cost_stt + len(llm_times) * cost_llm, 2),
        "by_tag": group(lambda r: r.tags or ["(none)"]),
        "by_speaker": group(lambda r: [r.speaker]) if any(r.speaker for r in results) else {},
        "pass_rules": {"min_correct": pass_correct, "max_silently_wrong": max_silently_wrong},
        "passed": bool(total) and rates["correct"] >= pass_correct and rates["silently_wrong"] <= max_silently_wrong,
    }


def _percent(value: float) -> str:
    return f"{value * 100:.1f}%"


def format_report(summary: dict, results: list[CaseResult], *, stage: str, stt: str, engine: str) -> str:
    """The report as text for the terminal."""
    s, total = summary, summary["total"]
    rules = s["pass_rules"]
    lines = [
        f"GetLekka accuracy report   stage: {stage}   speech: {stt if stage == 'voice' else '(skipped)'}   "
        f"AI: {engine}   {total} cases",
        "",
        f"  Correct, saved silently : {s['counts']['correct']:>4}/{total}  {_percent(s['rates']['correct']):>6}"
        f"   (need >= {_percent(rules['min_correct'])})",
        f"  Would ask the owner     : {s['counts']['asks']:>4}/{total}  {_percent(s['rates']['asks']):>6}   (friction, not harm)",
        f"  SILENTLY WRONG          : {s['counts']['silently_wrong']:>4}/{total}  {_percent(s['rates']['silently_wrong']):>6}"
        f"   (need <= {_percent(rules['max_silently_wrong'])}) <- the dangerous one",
        f"  Failed (engine error)   : {s['counts']['failed']:>4}/{total}  {_percent(s['rates']['failed']):>6}",
        "",
        f"  Intent understood right : {_percent(s['intent_accuracy'])}",
    ]
    if s["field_accuracy"]:
        lines.append("  Entry fields right      : " + "   ".join(
            f"{name} {_percent(value)}" for name, value in s["field_accuracy"].items()))
    lines.append(f"  Name spelling near-misses (duplicate-customer risk): {s['name_near_misses']}")
    if s["avg_cer"] is not None:
        lines.append(f"  Speech error (characters wrong): {_percent(s['avg_cer'])} on average   (0% = perfect)")
    timing = [f"speech {s['avg_stt_seconds']}s" if s["avg_stt_seconds"] is not None else None,
              f"AI {s['avg_llm_seconds']}s" if s["avg_llm_seconds"] is not None else None]
    if any(timing):
        lines.append("  Average time per case   : " + ", ".join(t for t in timing if t))
    if s["total_cost"]:
        lines.append(f"  Total cost of this run  : ₹{s['total_cost']}")

    lines += ["", "  By kind of sentence (correct / silently wrong):"]
    lines += [f"    {tag:<16} n={row['n']:<3} {_percent(row['correct']):>6}  {_percent(row['silently_wrong']):>6}"
              for tag, row in s["by_tag"].items()]
    if s["by_speaker"]:
        lines += ["", "  By speaker (correct / silently wrong / speech error):"]
        lines += [f"    {str(who):<16} n={row['n']:<3} {_percent(row['correct']):>6}  {_percent(row['silently_wrong']):>6}"
                  f"  {_percent(row['avg_cer']) if 'avg_cer' in row else '':>6}"
                  for who, row in s["by_speaker"].items()]

    problems = [r for r in results if r.outcome in ("silently_wrong", "failed")]
    if problems:
        lines += ["", f"  Problems to look at ({len(problems)}):"]
        for r in problems:
            who = f" [{r.speaker}]" if r.speaker else ""
            lines.append(f"    {r.case_id}{who}  {r.outcome.upper()}")
            lines.append(f"        should say: {r.said}")
            if r.transcript is not None:
                lines.append(f"        heard     : {r.transcript}   (speech error {_percent(r.cer or 0)})")
            lines.append(f"        expected  : {r.expected}")
            lines.append(f"        understood: {r.error or r.understood}")
    lines += ["", f"  RESULT: {'PASS' if s['passed'] else 'FAIL'}"]
    return "\n".join(lines)


def save_report(summary: dict, results: list[CaseResult], directory: str, label: str) -> str:
    """Save the full results as JSON (so runs can be compared later). Returns the file path."""
    folder = Path(directory)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{label}.json"
    path.write_text(json.dumps({"summary": summary, "results": [asdict(r) for r in results]},
                               ensure_ascii=False, indent=2), encoding="utf-8")
    return str(path)


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None, *, extract_fn=None, transcribe_fn=None) -> int:
    """Run the evaluation. Exit code 0 = PASS, 1 = FAIL, 2 = could not run."""
    parser = argparse.ArgumentParser(description="Accuracy and cost report for GetLekka.")
    parser.add_argument("--cases", default=DEFAULT_CASES, help="the test set (default: %(default)s)")
    parser.add_argument("--stage", choices=["text", "voice"], required=True,
                        help="text = sentences only; voice = your recordings")
    parser.add_argument("--audio-dir", default=DEFAULT_AUDIO_DIR, help="recordings folder (default: %(default)s)")
    parser.add_argument("--stt", choices=["local", "hosted"], help="speech engine (default: SPEECH_ENGINE setting)")
    parser.add_argument("--engine", choices=["local", "hosted"], help="AI engine (default: EXTRACTION_ENGINE setting)")
    parser.add_argument("--today", help="pretend today is this date, YYYY-MM-DD (default: the test set's date)")
    parser.add_argument("--limit", type=int, help="only the first N cases (a quick trial run)")
    parser.add_argument("--pass-correct", type=float, default=PASS_CORRECT)
    parser.add_argument("--max-silently-wrong", type=float, default=PASS_MAX_SILENTLY_WRONG)
    parser.add_argument("--cost-stt", type=float, default=0.0, help="price in rupees of ONE speech call")
    parser.add_argument("--cost-llm", type=float, default=0.0, help="price in rupees of ONE AI call")
    parser.add_argument("--out", default=DEFAULT_REPORT_DIR, help="where to save the JSON report")
    parser.add_argument("--no-save", action="store_true", help="do not save a JSON report")
    args = parser.parse_args(argv)

    try:
        cases, file_today = load_cases(args.cases)
        today = date.fromisoformat(args.today) if args.today else (file_today or DEFAULT_TODAY)
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as err:
        print(f"[FAILED] could not read the test set: {err}")
        return 2
    if args.limit:
        cases = cases[:args.limit]

    recordings = None
    if args.stage == "voice":
        recordings, unmatched = find_recordings(args.audio_dir, [c["id"] for c in cases])
        missing = [c["id"] for c in cases if c["id"] not in recordings]
        if not recordings:
            print(f"[FAILED] no recordings found in {args.audio_dir}. Name them like k01.ogg or k01__anita.m4a")
            return 2
        if missing:
            print(f"(no recording yet for {len(missing)} cases: {', '.join(missing[:10])}{'...' if len(missing) > 10 else ''})")
        if unmatched:
            print(f"(ignored files that match no case: {', '.join(unmatched[:10])})")

    extract_fn = extract_fn or (lambda text, day: extraction.extract(text, engine=args.engine, today=day))
    transcribe_fn = transcribe_fn or (lambda path: speech.transcribe(path, engine=args.stt))

    stt_name = args.stt or speech.get_engine().name
    llm_name = args.engine or extraction.get_engine().name
    results = run_evaluation(cases, stage=args.stage, transcribe_fn=transcribe_fn, extract_fn=extract_fn,
                             today=today, recordings=recordings)
    summary = summarize(results, pass_correct=args.pass_correct, max_silently_wrong=args.max_silently_wrong,
                        cost_stt=args.cost_stt, cost_llm=args.cost_llm)
    print(format_report(summary, results, stage=args.stage, stt=stt_name, engine=llm_name))
    if not args.no_save:
        print(f"\n  Saved: {save_report(summary, results, args.out, f'{args.stage}_{stt_name}_{llm_name}')}")
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
