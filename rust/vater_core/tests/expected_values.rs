//! task.formal_verification.015: the Rust port against the literal expected values that the
//! Python tests also use (tests/test_independent_expected_values.py). Neither suite computes
//! an expected value; both read them from the same fixture file.
use std::path::Path;
use vater_core::*;

const FIXTURE: &str = "../../tests/fixtures/independent_expected_values.txt";
const NOW_NS: u64 = 1_000_000;
const NOW_MS: u64 = 1_000;

fn tool_index(name: &str) -> u8 {
    match name {
        "checked_add" => 0,
        "checked_add_recorded" => 1,
        "github_test_create_issue" => 2,
        "delete_file" => 3,
        other => panic!("unknown tool {other}"),
    }
}

fn destination_index(name: &str) -> u8 {
    match name {
        "github_issues" => 0,
        "other_destination" => 1,
        other => panic!("unknown destination {other}"),
    }
}

fn mask(list: &str, index: fn(&str) -> u8) -> u64 {
    if list == "-" {
        return 0;
    }
    list.split(',').fold(0u64, |bits, name| bits | 1u64 << index(name))
}

fn deadline(word: &str) -> Option<u64> {
    match word {
        "-" => None,
        "future" => Some(NOW_NS + 1),
        "now" => Some(NOW_NS),
        "past" => Some(NOW_NS - 1),
        other => panic!("unknown deadline {other}"),
    }
}

fn run(fields: &[&str]) -> Result<(), String> {
    match fields {
        ["add", left, right, expected] => {
            let got = match checked_add(left.parse().unwrap(), right.parse().unwrap()) {
                Ok(sum) => format!("ok:{sum}"),
                Err(AddError::Overflow) => "OVERFLOW".to_string(),
                Err(AddError::OutOfRange) => "OUT_OF_RANGE".to_string(),
            };
            (got == *expected).then_some(()).ok_or(got)
        }
        ["transition", from, to, expected] => {
            let allowed = transition_allowed(State::from_name(from).unwrap(), State::from_name(to).unwrap());
            let got = if allowed { "allowed" } else { "forbidden" };
            (got == *expected).then_some(()).ok_or(got.to_string())
        }
        ["permit", publisher, tool, destination, revoked, expires, tools, destinations, expected] => {
            let grant = Grant {
                allowed_tools: mask(tools, tool_index),
                allowed_destinations: mask(destinations, destination_index),
                revoked: *revoked == "yes",
                expires_at_ns: deadline(expires),
            };
            let named = (*destination != "-").then(|| destination_index(destination));
            let decision = permits(&grant, *publisher == "local", tool_index(tool), named, NOW_NS);
            let got = if decision { "allow" } else { "deny" };
            (got == *expected).then_some(()).ok_or(got.to_string())
        }
        ["approval", differs, used, expired, expected] => {
            let operation = Binding { principal: 1, tool: 2, target: 3, resource_version: 4, arguments_digest: 5 };
            let mut bound = operation;
            match *differs {
                "none" | "absent" => {}
                "principal" => bound.principal = 9,
                "tool" => bound.tool = 9,
                "target" => bound.target = 9,
                "version" => bound.resource_version = 9,
                "arguments" => bound.arguments_digest = 9,
                other => panic!("unknown field {other}"),
            }
            let approval = Approval {
                binding: bound,
                expires_at_ms: if *expired == "yes" { NOW_MS } else { NOW_MS + 1 },
                consumed: *used == "yes",
            };
            let found = (*differs != "absent").then_some(&approval);
            let got = if approval_admits(&operation, found, NOW_MS) { "admit" } else { "refuse" };
            (got == *expected).then_some(()).ok_or(got.to_string())
        }
        other => panic!("unknown fixture row {other:?}"),
    }
}

#[test]
fn every_row_of_the_shared_fixture_matches_the_port() {
    let path = Path::new(env!("CARGO_MANIFEST_DIR")).join(FIXTURE);
    let text = std::fs::read_to_string(&path).expect("the shared fixture");
    let mut checked = 0;
    let mut failures = Vec::new();
    for (number, line) in text.lines().enumerate() {
        let line = line.trim();
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        let fields: Vec<&str> = line.split_whitespace().collect();
        checked += 1;
        if let Err(got) = run(&fields) {
            failures.push(format!("line {}: {line} -> {got}", number + 1));
        }
    }
    assert!(checked >= 80, "only {checked} rows");
    assert!(failures.is_empty(), "{}", failures.join("\n"));
}
