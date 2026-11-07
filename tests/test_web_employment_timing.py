import subprocess
from pathlib import Path


def run_employment_timing_assertions(assertions: str) -> None:
    module_path = Path("apps/web/app/lib/employment-timing.ts").resolve()
    runner = """
      const fs = require("fs");
      const ts = require("typescript");
      const source = fs.readFileSync(process.argv[1], "utf8");
      const javascript = ts.transpileModule(source, {
        compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 }
      }).outputText;
      const timingModule = { exports: {} };
      new Function("exports", "require", "module", javascript)(
        timingModule.exports,
        require,
        timingModule
      );
      const { formatJobStartTerm, formatJobTimingEvidence, isMissingActualTerm, jobTermFilterText } =
        timingModule.exports;
    """ + assertions
    subprocess.run(["node", "--eval", runner, str(module_path)], check=True)


def test_academic_term_display_uses_real_fixed_term_start_without_losing_evidence():
    run_employment_timing_assertions(
        """

      const observedTiming = {
        raw_label: "January 4 to April 23 2027",
        timing_type: "fixed_term",
        start_date: "2027-01-04"
      };
      if (formatJobStartTerm(observedTiming) !== "Winter 2027") process.exit(1);
      if (formatJobTimingEvidence(observedTiming) !== "January 4 to April 23 2027") process.exit(2);
      if (!jobTermFilterText(observedTiming).includes("winter 2027")) process.exit(3);
      if (!jobTermFilterText(observedTiming).includes("january 4 to april 23 2027")) process.exit(4);
        """
    )


def test_yearless_january_work_term_displays_inferred_winter_and_preserves_evidence():
    run_employment_timing_assertions(
        """
      for (const [raw_label, duration_months] of [["January - April", 4], ["January - August", 8]]) {
        const timing = { raw_label, timing_type: "fixed_term", start_date: "2027-01-01", duration_months };
        if (formatJobStartTerm(timing) !== "Winter 2027") process.exit(1);
        if (formatJobTimingEvidence(timing) !== raw_label) process.exit(1);
        if (isMissingActualTerm(timing)) process.exit(1);
        if (!jobTermFilterText(timing).includes("winter 2027")) process.exit(1);
      }
        """
    )


def test_spring_source_label_is_normalized_to_canonical_summer_coop_term():
    run_employment_timing_assertions(
        """
      const employerSpringLabel = {
        raw_label: "Spring 2027",
        timing_type: "fixed_term",
        start_date: "2027-05-01"
      };
      if (formatJobStartTerm(employerSpringLabel) !== "Summer 2027") process.exit(1);
      if (formatJobTimingEvidence(employerSpringLabel) !== "Spring 2027") process.exit(2);
      if (!jobTermFilterText(employerSpringLabel).includes("spring 2027")) process.exit(3);
      if (!jobTermFilterText(employerSpringLabel).includes("summer 2027")) process.exit(4);
        """
    )


def test_academic_term_display_does_not_treat_posted_date_fallback_as_job_term():
    run_employment_timing_assertions(
        """
      const postedDateFallback = {
        raw_label: "Posted January 4, 2027",
        timing_type: "posted_date_fallback",
        start_date: "2027-01-04"
      };
      if (formatJobStartTerm(postedDateFallback) !== "Posted January 4, 2027") process.exit(1);
        """
    )


def test_missing_actual_term_includes_unknown_and_posted_fallback_but_not_immediate():
    run_employment_timing_assertions(
        """
      if (!isMissingActualTerm({ timing_type: "unknown" })) process.exit(1);
      if (!isMissingActualTerm({
        raw_label: "Posted 2026-08-01",
        timing_type: "posted_date_fallback",
        start_date: "2026-08-01"
      })) process.exit(2);
      if (isMissingActualTerm({ raw_label: "Immediate", timing_type: "unknown" })) process.exit(3);
      if (isMissingActualTerm({
        raw_label: "Summer 2027",
        timing_type: "fixed_term",
        start_date: "2027-05-01"
      })) process.exit(4);
        """
    )


def test_duration_only_saved_postings_have_no_actual_start_term():
    # Minimized timing projections from observed saved co-op/intern postings.
    run_employment_timing_assertions(
        """
      for (const raw_label of ["8 Month", "4 month Co-op", "2–3 Months",
                               "8-16month Co-op", "9-Month", "8-month work term",
                               "12–16 week internship", "16 weeks", "8-month coop"]) {
        const timing = { raw_label, timing_type: "fixed_term", start_date: null };
        if (!isMissingActualTerm(timing)) throw new Error(raw_label);
        if (formatJobTimingEvidence(timing) !== raw_label) process.exit(2);
      }
        """
    )


def test_observed_late_august_summer_start_keeps_canonical_term():
    # The saved HR internship explicitly specifies Summer and an August 31 start.
    run_employment_timing_assertions(
        """
      const timing = {
        raw_label: "Summer 2026", timing_type: "fixed_term", start_date: "2026-08-31"
      };
      if (formatJobStartTerm(timing) !== "Summer 2026") process.exit(1);
      if (isMissingActualTerm(timing)) process.exit(2);
      if (jobTermFilterText(timing).includes("fall")) process.exit(3);
        """
    )
